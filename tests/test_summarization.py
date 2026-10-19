"""Tests for AI summarization module.

The summarizer's HTTP client is swapped for one over a fake transport, so what is
asserted is the chat-completions request put on the wire and how each answer the
server can give is reported.
"""

import json
from typing import Any

import httpx
import pytest

from houdini_mcp.tools import summarization


def completion(content: Any = "Test summary", finish_reason: str = "stop") -> dict:
    """An OpenAI chat-completions response body, shaped as the omlx server sends it."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": summarization.SUMMARIZATION_MODEL,
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
        self.production_client = summarization.summarizer_client
        self.sent: list[httpx.Request] = []
        self.response: httpx.Response | type[Exception] = httpx.Response(200, json=completion())

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        if isinstance(self.response, type):
            raise self.response("boom")
        return self.response

    def client(self) -> httpx.AsyncClient:
        """The production client, with this wire in place of the network."""
        real = self.production_client()
        return httpx.AsyncClient(
            base_url=real.base_url,
            headers=real.headers,
            timeout=real.timeout,
            transport=httpx.MockTransport(self.handle),
        )


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    """Route the summarizer through a fake transport that answers with a finished completion."""
    wire = Wire()
    monkeypatch.setattr(summarization, "summarizer_client", wire.client)
    monkeypatch.setattr(summarization, "SUMMARIZATION_ENABLED", True)
    return wire


class TestEstimateTokens:
    """Tests for token estimation."""

    def test_estimate_tokens_string(self):
        """Test token estimation for strings."""
        text = "a" * 100  # 100 characters
        assert summarization.estimate_tokens(text) == 25  # 100/4

    def test_estimate_tokens_dict(self):
        """Test token estimation for dict."""
        data = {"key": "value" * 10}
        result = summarization.estimate_tokens(data)
        assert result > 0

    def test_estimate_tokens_list(self):
        """Test token estimation for list."""
        data = [{"x": i} for i in range(100)]
        result = summarization.estimate_tokens(data)
        assert result > 100  # Should be more than 100 tokens for 100 items


@pytest.fixture
def no_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """A summarizer transport that fails the test if anything is sent on it."""

    def refuse(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"a summarization request was sent: {request.url}")

    monkeypatch.setattr(
        summarization,
        "summarizer_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
    )
    monkeypatch.setattr(summarization, "SUMMARIZATION_ENABLED", True)


SUMMARIZERS = [
    summarization.summarize_errors,
    summarization.summarize_geometry,
    summarization.summarize_scene,
]
RESULT = {"status": "success", "nodes": ["/obj/geo1/node"] * 50}


def threshold_below(monkeypatch: pytest.MonkeyPatch, data: dict, by: int) -> None:
    """Set AUTO_SUMMARIZE_THRESHOLD ``by`` tokens below the data's estimated size."""
    tokens = summarization.estimate_tokens(data)
    monkeypatch.setattr(summarization, "AUTO_SUMMARIZE_THRESHOLD", tokens - by)


class TestSummarized:
    """summarize is one bool: False is off, True summarizes what is over the threshold."""

    @pytest.mark.parametrize("summarizer", SUMMARIZERS)
    async def test_false_sends_nothing_however_large(self, no_wire, monkeypatch, summarizer):
        threshold_below(monkeypatch, RESULT, by=1)
        result = await summarization.summarized(dict(RESULT), summarizer, asked=False)
        assert result == RESULT

    @pytest.mark.parametrize("summarizer", SUMMARIZERS)
    async def test_true_summarizes_past_the_threshold(self, wire, monkeypatch, summarizer):
        threshold_below(monkeypatch, RESULT, by=1)
        result = await summarization.summarized(dict(RESULT), summarizer, asked=True)
        assert result == {**RESULT, "ai_summary": "Test summary", "_summarized": True}
        assert len(wire.sent) == 1

    @pytest.mark.parametrize("summarizer", SUMMARIZERS)
    async def test_true_leaves_a_result_at_the_threshold_as_is(
        self, no_wire, monkeypatch, summarizer
    ):
        threshold_below(monkeypatch, RESULT, by=0)
        result = await summarization.summarized(dict(RESULT), summarizer, asked=True)
        assert result == RESULT

    @pytest.mark.parametrize("summarizer", SUMMARIZERS)
    async def test_disabled_leaves_the_result_as_is(self, no_wire, monkeypatch, summarizer):
        """A setting, not a failure: no request, no ai_summary_error, no warning."""
        monkeypatch.setattr(summarization, "SUMMARIZATION_ENABLED", False)
        threshold_below(monkeypatch, RESULT, by=1)
        result = await summarization.summarized(dict(RESULT), summarizer, asked=True)
        assert result == RESULT

    @pytest.mark.parametrize(
        ("enabled", "asked", "by", "decided"),
        [
            (True, False, 1, False),
            (True, True, 0, False),
            (False, True, 1, False),
            (True, True, 1, True),
        ],
    )
    async def test_logs_the_decision_and_its_inputs(
        self, no_wire, monkeypatch, caplog, enabled, asked, by, decided
    ):
        monkeypatch.setattr(summarization, "SUMMARIZATION_ENABLED", enabled)
        threshold_below(monkeypatch, RESULT, by=by)
        tokens = summarization.estimate_tokens(RESULT)

        async def unchanged(data: dict) -> dict:
            return data

        with caplog.at_level("DEBUG", logger="houdini_mcp.summarization"):
            await summarization.summarized(dict(RESULT), unchanged, asked=asked)
        assert caplog.messages == [
            f"Summarize: {decided} (enabled={enabled}, asked={asked}, ~{tokens} tokens, "
            f"threshold {tokens - by})"
        ]


class TestDefaults:
    """With no configuration, summarization is on and aimed at the local completions server."""

    @pytest.fixture
    def defaults(self, monkeypatch):
        """The module as it loads with no SUMMARIZATION_* variable set, restored afterwards."""
        import importlib

        for name in (
            "SUMMARIZATION_ENABLED",
            "SUMMARIZATION_BASE_URL",
            "SUMMARIZATION_API_KEY",
            "SUMMARIZATION_MODEL",
        ):
            monkeypatch.delenv(name, raising=False)
        yield importlib.reload(summarization)
        monkeypatch.undo()
        importlib.reload(summarization)

    def test_enabled_against_the_local_server(self, defaults):
        """The out-of-the-box target is the omlx server and its instruct model."""
        assert defaults.SUMMARIZATION_ENABLED is True
        assert str(defaults.SUMMARIZATION_BASE_URL) == "http://100.66.66.66:8000/v1"
        assert defaults.SUMMARIZATION_MODEL == "mlx-community--Qwen3-30B-A3B-Instruct-2507-8bit"

    def test_client_authenticates_to_the_local_server(self, defaults):
        """The default client carries the server's key as a bearer token."""
        client = defaults.summarizer_client()
        assert str(client.base_url) == "http://100.66.66.66:8000/v1/"
        assert client.headers["Authorization"] == "Bearer 1234"
        assert client.timeout.read == defaults.REQUEST_TIMEOUT_SECONDS
        assert client.timeout.connect == defaults.CONNECT_TIMEOUT_SECONDS

    def test_anthropic_settings_change_nothing(self, defaults, monkeypatch):
        """An Anthropic key or proxy in the environment does not redirect the summarizer."""
        import importlib

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
        monkeypatch.setenv("CLAUDE_PROXY_URL", "http://localhost:8082")
        client = importlib.reload(summarization).summarizer_client()

        assert str(client.base_url) == "http://100.66.66.66:8000/v1/"
        assert client.headers["Authorization"] == "Bearer 1234"

    @pytest.mark.parametrize(
        "raw",
        [
            "http://host:abc/v1",
            "localhost:8000/v1",
            "100.66.66.66:8000/v1",
            "",
            "ftp://host/v1",
            "http://user:secret@host/v1",
        ],
    )
    def test_malformed_base_url_fails_at_import(self, defaults, monkeypatch, raw):
        """A base URL that is not a plain http(s) URL with a host stops the module loading."""
        import importlib

        monkeypatch.setenv("SUMMARIZATION_BASE_URL", raw)
        with pytest.raises(ValueError, match="SUMMARIZATION_BASE_URL"):
            importlib.reload(summarization)


class TestGetSummarizationStatus:
    """Tests for summarization status."""

    def test_get_summarization_status_returns_config(self):
        """Status names the server, the model, and the thresholds."""
        status = summarization.get_summarization_status()

        assert status == {
            "enabled": summarization.SUMMARIZATION_ENABLED,
            "model": summarization.SUMMARIZATION_MODEL,
            "base_url": str(summarization.SUMMARIZATION_BASE_URL),
            "auto_threshold_tokens": summarization.AUTO_SUMMARIZE_THRESHOLD,
            "target_summary_tokens": summarization.TARGET_SUMMARY_TOKENS,
        }


class TestComplete:
    """The chat-completions request the summarizer sends, and how each answer is reported."""

    async def test_sends_a_chat_completions_request(self, wire):
        """The request goes to /v1/chat/completions with the key, model, budget and prompt."""
        result = await summarization._complete("test prompt")

        assert result == summarization.Summary("Test summary")
        [request] = wire.sent
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {summarization.SUMMARIZATION_API_KEY}"
        assert json.loads(request.content) == {
            "model": summarization.SUMMARIZATION_MODEL,
            "max_tokens": summarization.TARGET_SUMMARY_TOKENS * 2,
            "messages": [{"role": "user", "content": "test prompt"}],
        }

    async def test_logs_the_summary_event(self, wire, caplog):
        """A finished summary is logged with the model that wrote it and its size."""
        with caplog.at_level("INFO", logger="houdini_mcp.summarization"):
            await summarization._complete("test prompt")

        assert f"Summarized with {summarization.SUMMARIZATION_MODEL} in " in caplog.text
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
            result = await summarization._complete("test prompt")

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
            result = await summarization._complete("test prompt")

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
            result = await summarization._complete("test prompt")

        assert isinstance(result, summarization.NoSummary)
        assert f"s: {logged}" in result.reason
        assert f"s: {logged}" in caplog.text

    async def test_failure_names_the_model_server_and_duration(self, wire, caplog):
        """A failure tells the log and the caller the model, server and time taken."""
        wire.response = httpx.ConnectTimeout

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarization._complete("test prompt")

        named = (
            f"{summarization.SUMMARIZATION_MODEL} at "
            f"{summarization.SUMMARIZATION_BASE_URL} failed after "
        )
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

        result = await summarization.summarize_geometry(geo_data)

        assert result["ai_summary"] == "Test summary"
        assert result["_summarized"] is True
        [request] = wire.sent
        assert "1000" in json.loads(request.content)["messages"][0]["content"]

    async def test_summarize_geometry_no_summary_on_error(self, wire):
        """When no summary comes back, the geometry data carries the reason instead."""
        wire.response = httpx.Response(500, text="Internal error")
        geo_data = {"point_count": 1000}

        result = await summarization.summarize_geometry(geo_data)

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

        result = await summarization.summarize_errors(error_data)

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

        result = await summarization.summarize_scene(scene_data)

        assert result["ai_summary"] == "Test summary"
        assert result["_summarized"] is True
