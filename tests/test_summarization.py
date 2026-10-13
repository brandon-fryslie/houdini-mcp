"""Tests for AI summarization module.

The summarizer talks through the real ``anthropic`` SDK; these tests swap the
SDK's transport for a fake so what is asserted is the request the SDK puts on
the wire and how each wire answer is reported.
"""

import json

import httpx2
import pytest
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient

from houdini_mcp.tools import summarization

MESSAGE_RESPONSE = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": ""},
        {"type": "text", "text": "Test summary"},
    ],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5},
}

MODEL_RESPONSE = {
    "id": "claude-haiku-5-5",
    "type": "model",
    "display_name": "Claude Haiku 5.5",
    "created_at": "2026-01-01T00:00:00Z",
    "lifecycle": "active",
}


def api_error(status: int, error_type: str) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": error_type}}
    )


class Wire:
    """The fake far end of the SDK's transport: every request sent, and one answer."""

    def __init__(self) -> None:
        self.sent: list[httpx2.Request] = []
        self.response: httpx2.Response | type[Exception] = httpx2.Response(
            200, json=MESSAGE_RESPONSE
        )

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.sent.append(request)
        if isinstance(self.response, type):
            raise self.response("boom")
        return self.response

    def client(self) -> AsyncAnthropic:
        return AsyncAnthropic(
            api_key="test-key",
            base_url="http://summarizer.test",
            max_retries=0,
            http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handle)),
        )


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> Wire:
    """Route the summarizer's client through a fake transport that answers with a message."""
    wire = Wire()
    monkeypatch.setattr(summarization, "anthropic_client", wire.client)
    monkeypatch.setattr(summarization, "SUMMARIZATION_ENABLED", True)
    return wire


def respond_with(wire: Wire, response: httpx2.Response | type[Exception]) -> None:
    wire.response = response


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


class TestShouldSummarize:
    """Tests for summarization decision logic."""

    def test_should_summarize_force_true(self):
        """Test force summarization."""
        assert summarization.should_summarize({}, force=True) is True

    def test_should_summarize_small_data(self):
        """Test small data doesn't get summarized."""
        small_data = {"count": 10}
        assert summarization.should_summarize(small_data) is False

    def test_should_summarize_large_data(self, monkeypatch):
        """Test large data triggers summarization."""
        monkeypatch.setattr(summarization, "SUMMARIZATION_ENABLED", True)
        # Create data larger than threshold (default 5000 tokens = ~20000 chars)
        large_data = {"data": "x" * 30000}
        assert summarization.should_summarize(large_data) is True

    def test_should_summarize_disabled(self, monkeypatch):
        """Test summarization disabled."""
        monkeypatch.setattr(summarization, "SUMMARIZATION_ENABLED", False)
        large_data = {"data": "x" * 30000}
        assert summarization.should_summarize(large_data) is False


class TestDefaults:
    """The out-of-the-box configuration targets what exists today."""

    def test_default_model_is_a_current_haiku(self, monkeypatch):
        """With no SUMMARIZATION_MODEL set, the module names the current small model."""
        monkeypatch.delenv("SUMMARIZATION_MODEL", raising=False)
        import importlib

        module = importlib.reload(summarization)
        assert module.SUMMARIZATION_MODEL == "claude-haiku-5-5"

    def test_client_does_not_retry(self, monkeypatch):
        """A summary inside a tool call is one attempt: the SDK's retries are off."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        client = summarization.anthropic_client()
        assert client.max_retries == 0
        assert client.timeout == summarization.REQUEST_TIMEOUT_SECONDS

    def test_no_private_proxy_default(self):
        """The upstream org's localhost proxy is not a default anywhere in the module."""
        import inspect

        assert "8082" not in inspect.getsource(summarization)
        assert not hasattr(summarization, "CLAUDE_PROXY_URL")


class TestCallClaude:
    """Tests for the request the summarizer sends and how answers are reported."""

    async def test_call_claude_disabled(self, wire, monkeypatch):
        """Disabled summarization sends nothing and returns None."""
        monkeypatch.setattr(summarization, "SUMMARIZATION_ENABLED", False)
        assert await summarization._call_claude("test prompt") is None
        assert wire.sent == []

    async def test_call_claude_sends_messages_request(self, wire):
        """The request names the configured model, the budget, and the prompt."""
        result = await summarization._call_claude("test prompt")

        assert result == "Test summary"
        [request] = wire.sent
        assert request.url.path == "/v1/messages"
        assert request.headers["x-api-key"] == "test-key"
        body = json.loads(request.content)
        assert body["model"] == summarization.SUMMARIZATION_MODEL
        assert body["max_tokens"] == summarization.TARGET_SUMMARY_TOKENS * 2
        assert body["thinking"] == {"type": "disabled"}
        assert body["output_config"] == {"effort": "low"}
        assert body["messages"] == [{"role": "user", "content": "test prompt"}]

    async def test_call_claude_logs_the_summary_event(self, wire, caplog):
        """A finished summary is logged with the model that wrote it and the tokens it cost."""
        with caplog.at_level("INFO", logger="houdini_mcp.summarization"):
            await summarization._call_claude("test prompt")

        assert "Summarized with claude-haiku-5-5: 10 input tokens, 5 output tokens" in caplog.text

    async def test_call_claude_drops_a_summary_the_model_did_not_finish(self, wire, caplog):
        """A response cut off by the cap is reported, not attached as a summary."""
        respond_with(
            wire,
            httpx2.Response(200, json={**MESSAGE_RESPONSE, "stop_reason": "max_tokens"}),
        )

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarization._call_claude("test prompt")

        assert result is None
        assert "Summarization stopped early (max_tokens) after 5 output tokens" in caplog.text

    async def test_call_claude_joins_text_blocks_only(self, wire):
        """Thinking blocks are not part of the summary; several text blocks are one string."""
        response = dict(MESSAGE_RESPONSE)
        response["content"] = [
            {"type": "thinking", "thinking": "", "signature": ""},
            {"type": "text", "text": "Part one. "},
            {"type": "text", "text": "Part two."},
        ]
        respond_with(wire, httpx2.Response(200, json=response))

        assert await summarization._call_claude("test prompt") == "Part one. Part two."

    @pytest.mark.parametrize(
        ("response", "cause"),
        [
            (api_error(401, "authentication_error"), "rejected_credentials"),
            (api_error(403, "permission_error"), "rejected_credentials"),
            (api_error(404, "not_found_error"), "unknown_model"),
            (api_error(500, "api_error"), "api_error"),
            (httpx2.ConnectError, "unreachable"),
            (httpx2.ReadTimeout, "unreachable"),
        ],
    )
    async def test_call_claude_failure_returns_none_and_logs_cause(
        self, wire, caplog, response, cause
    ):
        """A failed call yields no summary and a warning naming what to fix."""
        respond_with(wire, response)

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarization._call_claude("test prompt")

        assert result is None
        assert f"Summarization failed ({cause})" in caplog.text


class TestProbeEndpoint:
    """The status probe asks for the configured model and reports what it learned."""

    async def test_probe_reports_model_found(self, wire):
        """A found model reports ok with the endpoint and the model's lifecycle."""
        respond_with(wire, httpx2.Response(200, json=MODEL_RESPONSE))

        result = await summarization.probe_endpoint()

        [request] = wire.sent
        assert request.url.path == f"/v1/models/{summarization.SUMMARIZATION_MODEL}"
        assert result == {
            "ok": True,
            "base_url": "http://summarizer.test",
            "model_lifecycle": "active",
        }

    async def test_probe_reports_deprecated_lifecycle(self, wire):
        """A deprecated model is still found, and says so."""
        respond_with(wire, httpx2.Response(200, json={**MODEL_RESPONSE, "lifecycle": "deprecated"}))

        result = await summarization.probe_endpoint()

        assert result["ok"] is True
        assert result["model_lifecycle"] == "deprecated"

    @pytest.mark.parametrize(
        ("response", "cause"),
        [
            (api_error(401, "authentication_error"), "rejected_credentials"),
            (api_error(404, "not_found_error"), "unknown_model"),
            (api_error(529, "overloaded_error"), "api_error"),
            (httpx2.ConnectError, "unreachable"),
        ],
    )
    async def test_probe_reports_failure_cause(self, wire, response, cause):
        """A failed probe reports the cause and the SDK's message."""
        respond_with(wire, response)

        result = await summarization.probe_endpoint()

        assert result["ok"] is False
        assert result["cause"] == cause
        assert result["message"]

    async def test_probe_logs_what_it_learned(self, wire, caplog):
        """A probe logs the endpoint and model it found, or the cause it failed for."""
        respond_with(wire, httpx2.Response(200, json=MODEL_RESPONSE))
        with caplog.at_level("INFO", logger="houdini_mcp.summarization"):
            await summarization.probe_endpoint()
        assert (
            "Summarization endpoint http://summarizer.test serves claude-haiku-5-5"
            " (lifecycle active)" in caplog.text
        )

        respond_with(wire, api_error(401, "authentication_error"))
        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            await summarization.probe_endpoint()
        assert "Summarization endpoint probe failed (rejected_credentials)" in caplog.text

    async def test_probe_reports_missing_credentials(self, monkeypatch, tmp_path):
        """With nothing to authenticate as, the probe says so before any request is sent."""
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("ANTHROPIC_CONFIG_DIR", str(tmp_path))
        sent: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request)
            return httpx2.Response(200, json=MODEL_RESPONSE)

        monkeypatch.setattr(
            summarization,
            "anthropic_client",
            lambda: AsyncAnthropic(
                base_url="https://summarizer.test",
                http_client=DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
            ),
        )

        result = await summarization.probe_endpoint()

        assert result["ok"] is False
        assert result["cause"] == "no_credentials"
        assert sent == []


class TestGetSummarizationStatus:
    """Tests for summarization status."""

    async def test_get_summarization_status_returns_config_and_probe(self, wire):
        """Status carries the configuration and the probe result."""
        respond_with(wire, httpx2.Response(200, json=MODEL_RESPONSE))

        status = await summarization.get_summarization_status()

        assert status == {
            "enabled": True,
            "model": summarization.SUMMARIZATION_MODEL,
            "auto_threshold_tokens": summarization.AUTO_SUMMARIZE_THRESHOLD,
            "target_summary_tokens": summarization.TARGET_SUMMARY_TOKENS,
            "endpoint": {
                "ok": True,
                "base_url": "http://summarizer.test",
                "model_lifecycle": "active",
            },
        }


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
        """Test geometry data unchanged if Claude fails."""
        respond_with(wire, api_error(500, "api_error"))
        geo_data = {"point_count": 1000}

        result = await summarization.summarize_geometry(geo_data)

        assert "ai_summary" not in result
        assert "_summarized" not in result


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


class TestSummarizeRenderSettings:
    """Tests for render settings summarization."""

    async def test_summarize_render_settings_adds_summary(self, wire):
        """Test render settings summarization adds ai_summary field."""
        render_data = {
            "rop_type": "karma",
            "settings": {
                "samples": 64,
                "resolution": [1920, 1080],
            },
        }

        result = await summarization.summarize_render_settings(render_data)

        assert result["ai_summary"] == "Test summary"
        assert result["_summarized"] is True
