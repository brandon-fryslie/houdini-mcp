"""Tests for AI summarization module.

The summarizer's HTTP client is swapped for one over a fake transport, so what is
asserted is the chat-completions request put on the wire and how each answer the
server can give is reported.
"""

import dataclasses
import json
from typing import Any

import httpx
import pytest

from houdini_mcp.reading import RESPONSE_BOUND_BYTES, Reading, Unreadable
from houdini_mcp.settings import InvalidSetting, Summarizer, summarizer
from houdini_mcp.tools import summarization
from tests.conftest import SUMMARIZER, ClientBuilder, summarizer_through

DISABLED = dataclasses.replace(SUMMARIZER, enabled=False)


def completion(content: Any = "Test summary", finish_reason: str = "stop") -> dict:
    """An OpenAI chat-completions response body, shaped as the omlx server sends it."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": SUMMARIZER.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class Wire:
    """The fake far end of the summarizer's transport: every request sent, and one answer."""

    def __init__(self) -> None:
        self.sent: list[httpx.Request] = []
        self.response: httpx.Response | type[Exception] = httpx.Response(200, json=completion())

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        if isinstance(self.response, type):
            raise self.response("boom")
        return self.response


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch, production_summarizer_client: ClientBuilder) -> Wire:
    """Route the summarizer through a fake transport that answers with a finished completion."""
    wire = Wire()
    monkeypatch.setattr(
        summarization,
        "summarizer_client",
        summarizer_through(production_summarizer_client, wire.handle),
    )
    return wire


@pytest.fixture
def no_wire(monkeypatch: pytest.MonkeyPatch, production_summarizer_client: ClientBuilder) -> None:
    """A summarizer transport that fails the test if anything is sent on it."""

    def refuse(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"a summarization request was sent: {request.url}")

    monkeypatch.setattr(
        summarization, "summarizer_client", summarizer_through(production_summarizer_client, refuse)
    )


SUMMARIZERS = [
    summarization._summarize_errors,
    summarization._summarize_geometry,
    summarization._summarize_scene,
]
# A result read inline, and one too large to read whole: it arrives as a file.
SMALL = {"status": "success", "nodes": ["/obj/geo1/node"] * 50}
LARGE = {"status": "success", "nodes": ["/obj/geo1/node"] * 2000}


def at_the_bound() -> dict:
    """A result whose text is exactly the bound: the largest still read inline."""
    result = {"status": "success", "text": "x"}
    result["text"] = "x" * (RESPONSE_BOUND_BYTES - len(Reading.of(result).encoded) + 1)
    assert len(Reading.of(result).encoded) == RESPONSE_BOUND_BYTES
    return result


class TestSummarized:
    """summarize is one bool: False is off, True summarizes what is too large to read whole."""

    def test_the_results_straddle_the_bound(self):
        assert Reading.of(SMALL).fits and not Reading.of(LARGE).fits

    @pytest.mark.parametrize("summarize", SUMMARIZERS)
    async def test_false_sends_nothing_however_large(self, no_wire, summarize):
        result = await summarization.summarized(
            dict(LARGE), summarize, summarizer=SUMMARIZER, asked=False
        )
        assert result == LARGE

    @pytest.mark.parametrize("summarize", SUMMARIZERS)
    async def test_true_summarizes_a_result_too_large_to_read_whole(self, wire, summarize):
        result = await summarization.summarized(
            dict(LARGE), summarize, summarizer=SUMMARIZER, asked=True
        )
        assert result == {**LARGE, "ai_summary": "Test summary", "_summarized": True}
        assert len(wire.sent) == 1

    @pytest.mark.parametrize("summarize", SUMMARIZERS)
    async def test_true_leaves_a_result_at_the_bound_as_is(self, no_wire, summarize):
        result = at_the_bound()
        summarized = summarization.summarized(
            dict(result), summarize, summarizer=SUMMARIZER, asked=True
        )
        assert await summarized == result

    @pytest.mark.parametrize("summarize", SUMMARIZERS)
    async def test_disabled_leaves_the_result_as_is(self, no_wire, summarize):
        """A setting, not a failure: no request, no ai_summary_error, no warning."""
        result = await summarization.summarized(
            dict(LARGE), summarize, summarizer=DISABLED, asked=True
        )
        assert result == LARGE

    async def test_a_result_no_caller_can_read_is_refused_unsummarized(self, no_wire):
        with pytest.raises(Unreadable, match="not JSON-serializable"):
            await summarization.summarized(
                {"status": "success", "node": object()},
                summarization._summarize_scene,
                summarizer=SUMMARIZER,
                asked=True,
            )

    @pytest.mark.parametrize(
        ("enabled", "asked", "result", "decided"),
        [
            (True, False, LARGE, False),
            (True, True, at_the_bound(), False),
            (False, True, LARGE, False),
            (True, True, LARGE, True),
        ],
    )
    async def test_logs_the_decision_and_its_inputs(
        self, no_wire, caplog, enabled, asked, result, decided
    ):
        async def unchanged(data: dict, _summarizer: Summarizer) -> dict:
            return data

        configured = dataclasses.replace(SUMMARIZER, enabled=enabled)
        with caplog.at_level("DEBUG", logger="houdini_mcp.summarization"):
            await summarization.summarized(
                dict(result), unchanged, summarizer=configured, asked=asked
            )
        assert caplog.messages == [
            f"Summarize: {decided} (enabled={enabled}, asked={asked}, "
            f"{len(Reading.of(result).encoded)} bytes, bound {RESPONSE_BOUND_BYTES})"
        ]


class TestDefaults:
    """With no configuration, summarization is on and aimed at the local completions server."""

    def test_enabled_against_the_local_server(self):
        """The out-of-the-box target is the omlx server and its instruct model."""
        assert summarizer({}) == Summarizer(
            enabled=True,
            base_url="http://100.66.66.66:8000/v1",
            api_key="1234",
            model="mlx-community--Qwen3-30B-A3B-Instruct-2507-8bit",
        )

    def test_client_authenticates_to_the_local_server(self, production_summarizer_client):
        """The default client carries the server's key as a bearer token."""
        client = production_summarizer_client(summarizer({}))
        assert str(client.base_url) == "http://100.66.66.66:8000/v1/"
        assert client.headers["Authorization"] == "Bearer 1234"
        assert client.timeout.read == summarization.REQUEST_TIMEOUT_SECONDS
        assert client.timeout.connect == summarization.CONNECT_TIMEOUT_SECONDS

    def test_a_logged_summarizer_never_carries_its_key(self):
        assert "sk-secret" not in repr(summarizer({"SUMMARIZATION_API_KEY": "sk-secret"}))

    def test_anthropic_settings_change_nothing(self):
        """An Anthropic key or proxy in the environment does not redirect the summarizer."""
        environ = {"ANTHROPIC_API_KEY": "sk-ant-test", "CLAUDE_PROXY_URL": "http://localhost:8082"}
        assert summarizer(environ) == summarizer({})

    @pytest.mark.parametrize(
        ("raw", "told"),
        [
            ("http://host:abc/v1", "not a URL (Invalid port: 'abc')"),
            ("localhost:8000/v1", "must be an http(s) URL with a host"),
            ("100.66.66.66:8000/v1", "must be an http(s) URL with a host"),
            ("", "must be an http(s) URL with a host"),
            ("ftp://host/v1", "must be an http(s) URL with a host"),
        ],
    )
    def test_a_base_url_that_is_not_a_plain_http_url_is_refused(self, raw, told):
        """[LAW:no-silent-failure] it names the variable, what is wrong and what it held."""
        with pytest.raises(InvalidSetting) as refused:
            summarizer({"SUMMARIZATION_BASE_URL": raw})
        assert str(refused.value) == f"SUMMARIZATION_BASE_URL: Value error, {told}, got {raw!r}"

    @pytest.mark.parametrize(
        ("raw", "shown", "told"),
        [
            (
                "http://user:secret@host/v1",
                "http://***@host/v1",
                "carries credentials; set SUMMARIZATION_API_KEY",
            ),
            ("http://user:secret@host:abc/v1", "http://***@host:abc/v1", "not a URL"),
        ],
    )
    def test_a_base_url_carrying_credentials_is_refused_with_them_masked(self, raw, shown, told):
        """The URL is reported and logged, so it may hold no secret, and its refusal shows none."""
        with pytest.raises(InvalidSetting) as refused:
            summarizer({"SUMMARIZATION_BASE_URL": raw})
        assert str(refused.value).startswith(f"SUMMARIZATION_BASE_URL: Value error, {told}")
        assert str(refused.value).endswith(f", got {shown!r}")
        assert "secret" not in str(refused.value)

    @pytest.mark.parametrize(("raw", "enabled"), [("yes", True), ("ON", True), ("0", False)])
    def test_enabled_reads_any_word_for_on_or_off(self, raw, enabled):
        assert summarizer({"SUMMARIZATION_ENABLED": raw}).enabled is enabled

    @pytest.mark.parametrize("raw", ["maybe", ""])
    def test_enabled_refuses_a_word_that_is_neither(self, raw):
        with pytest.raises(InvalidSetting, match=f"^SUMMARIZATION_ENABLED: .*, got {raw!r}$"):
            summarizer({"SUMMARIZATION_ENABLED": raw})


class TestGetSummarizationStatus:
    """Tests for summarization status."""

    def test_get_summarization_status_returns_config(self):
        """Status names the server, the model, the bound a summary stands in past, and the summary's size."""
        status = summarization.get_summarization_status(summarizer=DISABLED)

        assert status == {
            "enabled": False,
            "model": SUMMARIZER.model,
            "base_url": SUMMARIZER.base_url,
            "response_bound_bytes": RESPONSE_BOUND_BYTES,
            "summary_bytes": summarization.SUMMARY_BYTES,
        }


class TestComplete:
    """The chat-completions request the summarizer sends, and how each answer is reported."""

    async def test_sends_a_chat_completions_request(self, wire):
        """The request goes to /v1/chat/completions with the key, model, budget and prompt."""
        result = await summarization._complete("test prompt", SUMMARIZER)

        assert result == summarization.Summary("Test summary")
        [request] = wire.sent
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {SUMMARIZER.api_key}"
        assert json.loads(request.content) == {
            "model": SUMMARIZER.model,
            "max_tokens": summarization.SUMMARY_TOKENS,
            "messages": [{"role": "user", "content": "test prompt"}],
        }

    async def test_logs_the_summary_event(self, wire, caplog):
        """A finished summary is logged with the model that wrote it and its size."""
        with caplog.at_level("INFO", logger="houdini_mcp.summarization"):
            await summarization._complete("test prompt", SUMMARIZER)

        assert f"Summarized with {SUMMARIZER.model} in " in caplog.text
        assert "s: 12 chars" in caplog.text

    @pytest.mark.parametrize(
        ("body", "reason"),
        [
            (completion("Cut off mid-sent", finish_reason="length"), "finish_reason=length, 16"),
            (completion(""), "finish_reason=stop, 0"),
            (completion(None), "finish_reason=stop, 0"),
        ],
    )
    async def test_drops_a_summary_the_model_did_not_finish(self, wire, caplog, body, reason):
        """A cut-off or empty completion is reported, not returned as a summary."""
        wire.response = httpx.Response(200, json=body)

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarization._complete("test prompt", SUMMARIZER)

        assert isinstance(result, summarization.NoSummary)
        assert result.reason.endswith(f"s: no finished summary: {reason} chars")
        assert "failed after " in caplog.text
        assert f"s: no finished summary: {reason} chars" in caplog.text

    async def test_server_error_logs_the_servers_message(self, wire, caplog):
        """An error status is logged with the server's own explanation."""
        wire.response = httpx.Response(
            507,
            json={"error": {"message": "Cannot load model: projected memory would exceed"}},
        )

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarization._complete("test prompt", SUMMARIZER)

        assert isinstance(result, summarization.NoSummary)
        assert "s: answered 507: " in result.reason
        assert "s: answered 507" in caplog.text
        assert "Cannot load model: projected memory would exceed" in caplog.text

    @pytest.mark.parametrize(
        ("response", "logged"),
        [
            (httpx.ConnectError, "ConnectError: boom"),
            (httpx.ReadTimeout, "ReadTimeout: boom"),
            (httpx.Response(200, text="not json"), "JSONDecodeError"),
            (httpx.Response(200, json={"choices": []}), "ValueError: not a chat completion"),
            (
                httpx.Response(200, json=completion([{"type": "text", "text": "parts"}])),
                "ValueError: not a text chat completion",
            ),
        ],
    )
    async def test_unreachable_or_malformed_answer_is_logged(self, wire, caplog, response, logged):
        """No answer, or one that is not a chat completion, yields its reason and a warning."""
        wire.response = response

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarization._complete("test prompt", SUMMARIZER)

        assert isinstance(result, summarization.NoSummary)
        assert f"s: {logged}" in result.reason
        assert f"s: {logged}" in caplog.text

    async def test_failure_names_the_model_server_and_duration(self, wire, caplog):
        """A failure tells the log and the caller the model, server and time taken."""
        wire.response = httpx.ConnectTimeout

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarization._complete("test prompt", SUMMARIZER)

        named = f"{SUMMARIZER.model} at {SUMMARIZER.base_url} failed after "
        assert isinstance(result, summarization.NoSummary)
        assert result.reason.startswith(named)
        assert f"Summarization by {named}" in caplog.text


class TestSummarizeGeometry:
    """Tests for geometry summarization."""

    async def test_summarize_geometry_adds_summary(self, wire):
        """Test geometry summarization adds ai_summary field."""
        geo_data = {
            "point_count": 1000,
            "primitive_count": 500,
            "bounding_box": {"min": [0, 0, 0], "max": [1, 1, 1]},
        }

        result = await summarization._summarize_geometry(geo_data, SUMMARIZER)

        assert result["ai_summary"] == "Test summary"
        assert result["_summarized"] is True
        [request] = wire.sent
        assert "1000" in json.loads(request.content)["messages"][0]["content"]

    async def test_summarize_geometry_no_summary_on_error(self, wire):
        """When no summary comes back, the geometry data carries the reason instead."""
        wire.response = httpx.Response(500, text="Internal error")
        geo_data = {"point_count": 1000}

        result = await summarization._summarize_geometry(geo_data, SUMMARIZER)

        assert result.keys() == {"point_count", "ai_summary_error"}
        assert result["ai_summary_error"].endswith("s: answered 500: Internal error")


class TestSummarizeErrors:
    """Tests for error summarization."""

    async def test_summarize_errors_adds_summary(self, wire):
        """Test error summarization adds ai_summary field."""
        error_data = {
            "error_nodes": [
                {"path": "/obj/geo1/bad_node", "errors": ["Missing input"]},
            ],
            "error_count": 1,
        }

        result = await summarization._summarize_errors(error_data, SUMMARIZER)

        assert result["ai_summary"] == "Test summary"
        assert result["_summarized"] is True
        [request] = wire.sent
        assert "/obj/geo1/bad_node" in json.loads(request.content)["messages"][0]["content"]


class TestSummarizeScene:
    """Tests for scene summarization."""

    async def test_summarize_scene_adds_summary(self, wire):
        """Test scene summarization adds ai_summary field."""
        scene_data = {
            "children": [
                {"name": "geo1", "type": "geo"},
                {"name": "geo2", "type": "geo"},
            ],
        }

        result = await summarization._summarize_scene(scene_data, SUMMARIZER)

        assert result["ai_summary"] == "Test summary"
        assert result["_summarized"] is True
