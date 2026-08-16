from __future__ import annotations


def test_batch_runner_constructs_agent_with_cli_platform(monkeypatch):
    import batch_runner

    captured = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run_conversation(self, prompt, task_id=None):
            return {
                "messages": [{"role": "assistant", "content": "ok"}],
                "completed": True,
                "api_calls": 1,
            }

        def _convert_to_trajectory_format(self, messages, prompt, completed):
            return []

    monkeypatch.setattr(batch_runner, "AIAgent", FakeAgent)
    monkeypatch.setattr(batch_runner, "sample_toolsets_from_distribution", lambda _name: [])

    config = {
        "distribution": "default",
        "model": "test-model",
        "max_iterations": 1,
    }
    result = batch_runner._process_single_prompt(0, {"prompt": "hello"}, 1, config)

    assert result["success"] is True
    assert captured["platform"] == "cli"
