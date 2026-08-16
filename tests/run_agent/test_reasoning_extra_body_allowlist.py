"""Tripwire for the reasoning extra_body host allowlist."""


def test_supports_reasoning_extra_body_allowlist():
    """OpenRouter forwards unknown fields, but other routes reject `reasoning`
    with 400s — the allowlist is deliberate. If a host is added/removed,
    update this list explicitly (mirrors the qwen endpoint-list tripwire)."""
    from run_agent import AIAgent

    class Stub:
        provider = ""
        model = "gpt-5.6"
        _base_url_lower = ""
        _lmstudio_reasoning_options_cached = lambda self: []
        _ollama_supports_thinking_cached = lambda self: False

        def _is_openrouter_url(self):
            return False

    allowed = (
        "https://nousresearch.com/v1",
        "https://ai-gateway.vercel.sh/v1",
        "https://models.github.ai/v1",
        "https://githubcopilot.com/v1",
    )
    for host in allowed:
        Stub._base_url_lower = host
        assert AIAgent._supports_reasoning_extra_body(Stub()), host
    Stub._base_url_lower = "https://unknown-proxy.example.com/v1"
    assert not AIAgent._supports_reasoning_extra_body(Stub())
