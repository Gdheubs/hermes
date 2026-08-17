"""Deterministic local runtime probe used only for focused integration testing."""

from lin_runtime.app import create_app


class ProbeAgent:
    def __init__(self, **kwargs):
        self.tool_progress_callback = kwargs["tool_progress_callback"]
        self._interrupt_requested = False

    def run_conversation(self, message, **kwargs):
        self.tool_progress_callback("tool.started", "terminal", "printf ok", {"command": "printf ok"})
        self.tool_progress_callback(
            "tool.completed", "terminal", None, None,
            duration=0.01, is_error=False, result="ok",
        )
        return {"final_response": "ok"}


app = create_app(agent_factory=ProbeAgent, service_token="local-secret")
