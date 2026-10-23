"""Tests for AI summarization module.

The summarizer's HTTP client is swapped for one over a fake transport, so what is
asserted is the chat-completions request put on the wire and how each answer the
server can give is reported.
"""

import dataclasses
import json

import httpx
import pytest

from houdini_mcp import shorthand
from houdini_mcp.reading import RESPONSE_BOUND_BYTES, Reading, Unreadable
from houdini_mcp.settings import InvalidSetting, Summarizer, summarizer
from houdini_mcp.tools import summarization
from houdini_mcp.tools.summarization import EXCERPT_BYTES, Excerpt
from tests.conftest import SUMMARIZER, completion

DISABLED = dataclasses.replace(SUMMARIZER, enabled=False)

BRIEFS = pytest.mark.parametrize(
    "brief",
    [summarization.ERRORS_BRIEF, summarization.GEOMETRY_BRIEF, summarization.SCENE_BRIEF],
)
# A result read inline, and one too large to read whole: it arrives as a file.
SMALL = {"status": "success", "nodes": ["/obj/geo1/node"] * 50}
LARGE = {"status": "success", "nodes": ["/obj/geo1/node"] * 2000}


def at_the_bound() -> dict:
    """A result whose text is exactly the bound: the largest still read inline."""
    result = {"status": "success", "text": "x"}
    result["text"] = "x" * (RESPONSE_BOUND_BYTES - len(Reading.of(result).encoded) + 1)
    assert len(Reading.of(result).encoded) == RESPONSE_BOUND_BYTES
    return result


def past_the_bound() -> dict:
    """The smallest result too large to read whole: one byte past the bound."""
    result = at_the_bound()
    result["text"] += "x"
    return result


class TestSummarized:
    """summarize is one bool: False is off, True summarizes what is too large to read whole."""

    def test_the_results_straddle_the_bound(self):
        assert Reading.of(SMALL).fits and not Reading.of(LARGE).fits

    @BRIEFS
    async def test_false_sends_nothing_however_large(self, no_wire, brief):
        result = await summarization.summarized(
            dict(LARGE), brief, summarizer=SUMMARIZER, asked=False
        )
        assert result == LARGE

    @BRIEFS
    async def test_true_summarizes_a_result_too_large_to_read_whole(self, wire, brief):
        result = await summarization.summarized(
            dict(LARGE), brief, summarizer=SUMMARIZER, asked=True
        )
        assert result == {
            **LARGE,
            "ai_summary": "Test summary",
            "summarized_bytes": EXCERPT_BYTES,
        }
        assert len(wire.sent) == 1

    @BRIEFS
    async def test_true_leaves_a_result_at_the_bound_as_is(self, no_wire, brief):
        result = at_the_bound()
        summarized = summarization.summarized(
            dict(result), brief, summarizer=SUMMARIZER, asked=True
        )
        assert await summarized == result

    @BRIEFS
    async def test_disabled_leaves_the_result_as_is(self, no_wire, brief):
        """A setting, not a failure: no request, no ai_summary_error, no warning."""
        result = await summarization.summarized(dict(LARGE), brief, summarizer=DISABLED, asked=True)
        assert result == LARGE

    @BRIEFS
    async def test_a_summary_that_does_not_come_back_leaves_only_its_reason(self, wire, brief):
        """No summary covers any of the result, so none says how much."""
        wire.response = httpx.Response(500, text="Internal error")

        result = await summarization.summarized(
            dict(LARGE), brief, summarizer=SUMMARIZER, asked=True
        )

        assert result.keys() == {*LARGE, "ai_summary_error"}
        assert result["ai_summary_error"].endswith("s: answered 500: Internal error")

    async def test_a_result_no_caller_can_read_is_refused_unsummarized(self, no_wire):
        with pytest.raises(Unreadable, match="not JSON-serializable"):
            await summarization.summarized(
                {"status": "success", "node": object()},
                summarization.SCENE_BRIEF,
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
        self, wire, caplog, enabled, asked, result, decided
    ):
        configured = dataclasses.replace(SUMMARIZER, enabled=enabled)
        with caplog.at_level("DEBUG", logger="houdini_mcp.summarization"):
            await summarization.summarized(
                dict(result), "Summarize this.", summarizer=configured, asked=asked
            )
        assert [m for m in caplog.messages if m.startswith("Summarize:")] == [
            f"Summarize: {decided} (enabled={enabled}, asked={asked}, "
            f"{len(Reading.of(result).encoded)} bytes, bound {RESPONSE_BOUND_BYTES})"
        ]
        assert len(wire.sent) == decided


class TestCoverage:
    """A summary is written from the start of the result's text, and both the request and
    the response say how much of it."""

    @BRIEFS
    async def test_the_request_says_it_is_part_of_the_result(self, wire, brief):
        """The summarizer is sent the brief, the key to the shorthand, the excerpt, and
        how much of the whole that is."""
        whole = Reading.of(LARGE).encoded
        await summarization.summarized(dict(LARGE), brief, summarizer=SUMMARIZER, asked=True)

        [prompt] = wire.prompts
        assert prompt.startswith(brief)
        assert shorthand.KEY in prompt
        assert whole[:EXCERPT_BYTES].decode() in prompt
        assert whole[: EXCERPT_BYTES + 1].decode() not in prompt
        assert f"is {len(whole)} bytes" in prompt
        assert f"first {EXCERPT_BYTES}" in prompt

    @BRIEFS
    async def test_the_response_says_how_much_its_file_the_summary_read(self, wire, brief):
        """The file holds the summarized result's text, and its first summarized_bytes
        bytes are what the summary was written from."""
        result = await summarization.summarized(
            dict(LARGE), brief, summarizer=SUMMARIZER, asked=True
        )

        file = Reading.of(result).encoded
        covered = result["summarized_bytes"]
        [prompt] = wire.prompts
        assert covered < len(Reading.of(LARGE).encoded)
        assert f"\n{file[:covered].decode()}\n```" in prompt

    async def test_the_smallest_result_summarized_is_summarized_from_part_of_it(self, wire):
        """So the request's "the rest is not shown" is true of every summary."""
        result = past_the_bound()

        summarized = await summarization.summarized(
            dict(result), summarization.SCENE_BRIEF, summarizer=SUMMARIZER, asked=True
        )

        assert summarized["summarized_bytes"] < len(Reading.of(result).encoded)

    def test_a_character_the_cut_splits_is_left_out(self):
        """The excerpt is whole characters, and covered counts the bytes it is."""
        result = {"status": "success", "text": "x"}
        before = len(Reading.of(result).encoded) - 1
        # The first byte of a three-byte character is the last byte the cut keeps.
        result["text"] = "x" * (EXCERPT_BYTES - 1 - before) + "€" * 3000
        reading = Reading.of(result)
        assert reading.encoded[EXCERPT_BYTES - 1 : EXCERPT_BYTES + 2] == "€".encode()

        excerpt = Excerpt.of(reading)

        assert (excerpt.covered, excerpt.total) == (EXCERPT_BYTES - 1, len(reading.encoded))
        assert excerpt.text.encode() == reading.encoded[: EXCERPT_BYTES - 1]

    def test_a_text_within_the_budget_is_excerpted_whole(self):
        reading = Reading.of(SMALL)
        assert Excerpt.of(reading) == Excerpt(
            reading.text, len(reading.encoded), len(reading.encoded)
        )

    @BRIEFS
    async def test_the_summary_event_says_how_much_it_read(self, wire, caplog, brief):
        with caplog.at_level("INFO", logger="houdini_mcp.summarization"):
            await summarization.summarized(dict(LARGE), brief, summarizer=SUMMARIZER, asked=True)

        [event] = caplog.messages
        assert event.startswith(
            f"Summarized the first {EXCERPT_BYTES} of {len(Reading.of(LARGE).encoded)} bytes "
            f"with {SUMMARIZER.model} in "
        )
        assert event.endswith("s: 12 chars")


class TestDefaults:
    """With no configuration, summarization is on and aimed at the local completions server."""

    def test_enabled_against_the_local_server(self):
        """The out-of-the-box target is the omlx server and the model it keeps pinned."""
        assert summarizer({}) == Summarizer(
            enabled=True,
            base_url="http://100.66.66.66:8000/v1",
            api_key="1234",
            model="Qwen3.8-Flash-Next-Sushi",
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
            ("http://host:abc/v1", "not a URL"),
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
            (
                "user:secret@host:8000/v1",
                "***@host:8000/v1",
                "must be an http(s) URL with a host",
            ),
            ("http:/user:secret@host/v1", "***@host/v1", "must be an http(s) URL with a host"),
            ("http://user:se/cret@host/v1", "http://***@host/v1", "not a URL"),
            ("http://user:secret/@host/v1", "http://***@host/v1", "not a URL"),
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


class TestSummarize:
    """The chat-completions request the summarizer sends, and how each answer is reported."""

    async def test_sends_a_chat_completions_request(self, wire):
        """The request goes to /v1/chat/completions with the key, model, budget and prompt,
        and switches the model's thinking off."""
        reading = Reading.of(LARGE)
        result = await summarization._summarize("Summarize this.", reading, SUMMARIZER)

        assert result == summarization.Summary("Test summary", EXCERPT_BYTES)
        [request] = wire.sent
        assert request.method == "POST"
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {SUMMARIZER.api_key}"
        assert json.loads(request.content) == {
            "model": SUMMARIZER.model,
            "max_tokens": summarization.SUMMARY_TOKENS,
            "messages": [
                {
                    "role": "user",
                    "content": summarization._prompt("Summarize this.", Excerpt.of(reading)),
                }
            ],
            "chat_template_kwargs": {"enable_thinking": False},
        }

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
            result = await summarize_large()

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
            result = await summarize_large()

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
            result = await summarize_large()

        assert isinstance(result, summarization.NoSummary)
        assert f"s: {logged}" in result.reason
        assert f"s: {logged}" in caplog.text

    async def test_failure_names_the_model_server_and_duration(self, wire, caplog):
        """A failure tells the log and the caller the model, server and time taken."""
        wire.response = httpx.ConnectTimeout

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarize_large()

        named = f"{SUMMARIZER.model} at {SUMMARIZER.base_url} failed after "
        assert isinstance(result, summarization.NoSummary)
        assert result.reason.startswith(named)
        read = f"the first {EXCERPT_BYTES} of {len(Reading.of(LARGE).encoded)} bytes"
        assert f"Summarization of {read} by {named}" in caplog.text


async def summarize_large() -> summarization.Summary | summarization.NoSummary:
    return await summarization._summarize("Summarize this.", Reading.of(LARGE), SUMMARIZER)
