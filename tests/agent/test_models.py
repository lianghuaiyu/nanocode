from nanocode.agent import models


def test_context_window():
    assert models._get_context_window("claude-opus-4-6") == 200000
    assert models._get_context_window("unknown-model") == 200000
    assert models._get_context_window("gpt-4o") == 128000


def test_thinking_support():
    assert models._model_supports_thinking("claude-opus-4-6") is True
    assert models._model_supports_thinking("claude-3-5-sonnet") is False
    assert models._model_supports_adaptive_thinking("claude-opus-4-6") is True
    assert models._model_supports_adaptive_thinking("claude-haiku-4-5-20251001") is False


def test_max_output():
    assert models._get_max_output_tokens("claude-opus-4-6") == 64000
    assert models._get_max_output_tokens("claude-sonnet-4-6") == 32000
    assert models._get_max_output_tokens("gpt-4o") == 16384


def test_retryable():
    class E429(Exception):
        status_code = 429

    assert models._is_retryable(E429()) is True
    assert models._is_retryable(ValueError("nope")) is False
    assert models._is_retryable(Exception("overloaded")) is True


def test_to_openai_tools():
    tools = [{"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    out = models._to_openai_tools(tools)
    assert out[0]["type"] == "function"
    assert out[0]["function"]["name"] == "t"
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}
