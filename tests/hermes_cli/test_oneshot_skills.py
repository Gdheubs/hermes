from types import SimpleNamespace

import pytest


def test_apply_preloaded_skills_uses_session_and_preserves_existing_prompt(monkeypatch):
    from agent import skill_commands
    from hermes_cli.oneshot import _apply_preloaded_skills

    captured = {}

    def fake_build(skills, task_id=None):
        captured["skills"] = skills
        captured["task_id"] = task_id
        return "loaded skill prompt", ["alpha", "beta"], []

    monkeypatch.setattr(skill_commands, "build_preloaded_skills_prompt", fake_build)
    agent = SimpleNamespace(
        session_id="session-123",
        ephemeral_system_prompt="existing prompt",
    )

    loaded = _apply_preloaded_skills(agent, ["alpha,beta", "alpha"])

    assert loaded == ["alpha", "beta"]
    assert captured == {
        "skills": ["alpha", "beta"],
        "task_id": "session-123",
    }
    assert agent.ephemeral_system_prompt == "existing prompt\n\nloaded skill prompt"
    assert agent.preloaded_skills == ["alpha", "beta"]


def test_apply_preloaded_skills_rejects_fully_unknown_request(monkeypatch):
    from agent import skill_commands
    from hermes_cli.oneshot import _apply_preloaded_skills

    monkeypatch.setattr(
        skill_commands,
        "build_preloaded_skills_prompt",
        lambda skills, task_id=None: ("", [], skills),
    )
    agent = SimpleNamespace(
        session_id="session-123",
        ephemeral_system_prompt=None,
    )

    with pytest.raises(ValueError, match=r"Unknown skill\(s\): missing-skill"):
        _apply_preloaded_skills(agent, "missing-skill")

    assert agent.ephemeral_system_prompt is None


def test_run_oneshot_forwards_skills_to_agent(monkeypatch, capsys):
    import hermes_cli.oneshot as oneshot

    captured = {}

    def fake_run_agent(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return "ok", {"final_response": "ok", "failed": False, "partial": False}

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)

    assert oneshot.run_oneshot("hello", skills=["alpha,beta"]) == 0
    assert captured["prompt"] == "hello"
    assert captured["skills"] == ["alpha,beta"]
    assert capsys.readouterr().out == "ok\n"


def test_main_oneshot_wrapper_forwards_skills(monkeypatch):
    import hermes_cli.main as main
    import hermes_cli.oneshot as oneshot

    captured = {}

    def fake_run_oneshot(prompt, **kwargs):
        captured["prompt"] = prompt
        captured.update(kwargs)
        return 0

    class ExitCalled(Exception):
        pass

    monkeypatch.setattr(oneshot, "run_oneshot", fake_run_oneshot)
    monkeypatch.setattr(main, "_cleanup_oneshot_runtime", lambda: None)
    monkeypatch.setattr(
        main,
        "_exit_after_oneshot",
        lambda rc: (_ for _ in ()).throw(ExitCalled(rc)),
    )

    with pytest.raises(ExitCalled):
        main._run_and_exit_oneshot("hello", skills=["alpha"])

    assert captured["prompt"] == "hello"
    assert captured["skills"] == ["alpha"]
