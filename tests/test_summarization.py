"""Tests for AI summarization module.

The summarizer's HTTP client is swapped for one over a fake transport, so what is
asserted is the chat-completions request put on the wire and how each answer the
server can give is reported.
"""

import json

import httpx
import pytest

from houdini_mcp.tools import summarization


def completion(content: str | None = "Test summary", finish_reason: str = "stop") -> dict:
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
        assert defaults.SUMMARIZATION_BASE_URL == "http://100.66.66.66:8000/v1"
        assert defaults.SUMMARIZATION_MODEL == "mlx-community--Qwen3-30B-A3B-Instruct-2507-8bit"

    def test_client_authenticates_to_the_local_server(self, defaults):
        """The default client carries the server's key as a bearer token."""
        client = defaults.summarizer_client()
        assert str(client.base_url) == "http://100.66.66.66:8000/v1/"
        assert client.headers["Authorization"] == "Bearer 1234"
        assert client.timeout.read == defaults.REQUEST_TIMEOUT_SECONDS

    def test_no_anthropic_path(self):
        """Nothing in the module names the Anthropic API, its proxy, or its credential."""
        import inspect

        source = inspect.getsource(summarization)
        for gone in ("anthropic", "8082", "ANTHROPIC_API_KEY", "CLAUDE_PROXY_URL", "/v1/messages"):
            assert gone not in source


class TestGetSummarizationStatus:
    """Tests for summarization status."""

    def test_get_summarization_status_returns_config(self):
        """Status names the server, the model, and the thresholds."""
        status = summarization.get_summarization_status()

        assert status == {
            "enabled": summarization.SUMMARIZATION_ENABLED,
            "model": summarization.SUMMARIZATION_MODEL,
            "base_url": summarization.SUMMARIZATION_BASE_URL,
            "auto_threshold_tokens": summarization.AUTO_SUMMARIZE_THRESHOLD,
            "target_summary_tokens": summarization.TARGET_SUMMARY_TOKENS,
        }


class TestComplete:
    """The chat-completions request the summarizer sends, and how each answer is reported."""

    async def test_disabled_sends_nothing(self, wire, monkeypatch):
        """Disabled summarization sends nothing and returns None."""
        monkeypatch.setattr(summarization, "SUMMARIZATION_ENABLED", False)
        assert await summarization._complete("test prompt") is None
        assert wire.sent == []

    async def test_sends_a_chat_completions_request(self, wire):
        """The request goes to /v1/chat/completions with the key, model, budget and prompt."""
        result = await summarization._complete("test prompt")

        assert result == "Test summary"
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

        assert result is None
        assert f"Summarization returned no finished summary: {reason} chars" in caplog.text

    async def test_server_error_logs_the_servers_message(self, wire, caplog):
        """An error status is logged with the server's own explanation."""
        wire.response = httpx.Response(
            507,
            json={"error": {"message": "Cannot load model: projected memory would exceed"}},
        )

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarization._complete("test prompt")

        assert result is None
        assert "/v1/chat/completions answered 507" in caplog.text
        assert "Cannot load model: projected memory would exceed" in caplog.text

    @pytest.mark.parametrize(
        ("response", "logged"),
        [
            (httpx.ConnectError, "ConnectError: boom"),
            (httpx.ReadTimeout, "ReadTimeout: boom"),
            (httpx.Response(200, text="not json"), "JSONDecodeError"),
            (httpx.Response(200, json={"choices": []}), "ValueError: not a chat completion"),
        ],
    )
    async def test_unreachable_or_malformed_answer_is_logged(self, wire, caplog, response, logged):
        """No answer, or one that is not a chat completion, yields None and a warning."""
        wire.response = response

        with caplog.at_level("WARNING", logger="houdini_mcp.summarization"):
            result = await summarization._complete("test prompt")

        assert result is None
        assert f"Summarization failed: {logged}" in caplog.text


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
        """Geometry data is unchanged when no summary comes back."""
        wire.response = httpx.Response(500, text="Internal error")
        geo_data = {"point_count": 1000}

        result = await summarization.summarize_geometry(geo_data)

        assert result == {"point_count": 1000}


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
