"""The background review's tokens must reach the session it shares an id with.

The review fork is deliberately persistence-isolated (``_persist_disabled``,
``_session_db = None``): it shares the parent's ``session_id`` for prompt-cache
warmth, so letting it write MESSAGES there is the curator-takeover bug — see
tests/test_background_review_session_isolation.py, which must keep passing.

Its TOKENS are a different matter. The provider bills the review's calls and the
observability backend files them under that same session id, so leaving them out
of state.db makes the session's own cost read low (measured 10-15% of a
gpt-5.4-mini pass, 27-29% of a claude-sonnet-4-6 one). ``_token_accounting_db``
is the narrow channel that carries counters — and only counters — across the
isolation boundary.

Two things have to hold together, and these tests pin both:
  * the fork accounts on the OWNER's store and session id, and
  * nothing about that re-arms a message-write path.
"""

import threading
from unittest.mock import MagicMock, patch


def _make_agent_stub(agent_cls, session_db=None):
    """Minimal AIAgent-like object with just enough state for _spawn_background_review."""
    agent = object.__new__(agent_cls)
    agent.model = "test-model"
    agent.platform = "test"
    agent.provider = "openai"
    agent.session_id = "sess-123"
    agent.quiet_mode = True
    agent._memory_store = None
    agent._memory_enabled = True
    agent._user_profile_enabled = False
    agent._memory_nudge_interval = 5
    agent._skill_nudge_interval = 5
    agent.background_review_callback = None
    agent.status_callback = None
    agent._cached_system_prompt = None
    agent._session_db = session_db
    import datetime as _dt
    agent.session_start = _dt.datetime(2026, 1, 1, 12, 0, 0)
    agent._MEMORY_REVIEW_PROMPT = "review memory"
    agent._SKILL_REVIEW_PROMPT = "review skills"
    agent._COMBINED_REVIEW_PROMPT = "review both"
    agent.enabled_toolsets = ["memory", "skills"]
    agent.disabled_toolsets = []
    return agent


class _SlowThread:
    """Thread stand-in: runs the target on .start(), reports alive until joined."""

    def __init__(self, *, target=None, daemon=None, name=None):
        self._target = target
        self._alive = False
        self.name = name

    def start(self):
        self._alive = True
        if self._target:
            self._target()

    def is_alive(self):
        return self._alive

    def join(self, timeout=None):
        self._alive = False


class TestAccountingChannelIsHonoured:
    """conversation_loop must account on the borrowed store when one is set, and
    must not try to CREATE a row it does not own."""

    def test_defaults_to_the_agents_own_store(self):
        import run_agent

        agent = object.__new__(run_agent.AIAgent)
        assert getattr(agent, "_token_accounting_db", None) is None, (
            "the channel must default to unset so ordinary agents keep accounting "
            "on their own _session_db / session_id"
        )

    def test_isolated_fork_keeps_message_writes_hard_stopped(self):
        """The channel must not become a back door into the message log."""
        import run_agent

        db = MagicMock()
        agent = object.__new__(run_agent.AIAgent)
        agent._session_db = None
        agent._persist_disabled = True
        agent._token_accounting_db = db
        agent._token_accounting_session_id = "sess-123"
        agent.session_id = "sess-123"

        agent._flush_messages_to_session_db([{"role": "user", "content": "hi"}], [])

        db.append_message.assert_not_called()
        db.create_session.assert_not_called()
        db.queue_token_counts.assert_not_called()


class TestForkWiring:
    def test_fork_accounts_on_the_parents_store_and_session(self):
        """Behavioural: run the real fork setup and read the attributes it set."""
        import run_agent

        parent_db = object()
        agent = _make_agent_stub(run_agent.AIAgent, session_db=parent_db)
        agent._credential_pool = None
        agent._background_review_lock = threading.Lock()
        agent._background_review_agent = None
        agent._active_children_lock = threading.Lock()
        agent._active_children = []

        forks = []

        def _fake_init(self, *args, **kwargs):
            forks.append(self)

        def _boom(self, *args, **kwargs):
            raise RuntimeError("stop before the review actually runs")

        with patch.object(run_agent.AIAgent, "__init__", _fake_init), \
             patch.object(run_agent.AIAgent, "run_conversation", _boom), \
             patch("threading.Thread", _SlowThread):
            agent._spawn_background_review(messages_snapshot=[], review_memory=True,
                                           review_skills=True)

        assert forks, "the review fork was never constructed"
        fork = forks[0]
        assert fork._persist_disabled is True, "message isolation must survive"
        assert fork._session_db is None, "the fork must not hold a message-writing store"
        assert fork._token_accounting_db is parent_db, (
            "the fork must account on the PARENT's store"
        )
        assert fork._token_accounting_session_id == agent.session_id, (
            "and against the session id it shares, so the counters land on the "
            "row the provider's billing is attributed to"
        )


class TestWaitForBackgroundReview:
    def test_spawn_keeps_a_handle(self):
        """Without a handle an owner tearing down shared state can only close blind."""
        import run_agent

        agent = _make_agent_stub(run_agent.AIAgent, session_db=object())

        def _noop_init(self, *args, **kwargs):
            raise RuntimeError("stop")

        with patch.object(run_agent.AIAgent, "__init__", _noop_init), \
             patch("threading.Thread", _SlowThread):
            agent._spawn_background_review(messages_snapshot=[], review_memory=True,
                                           review_skills=False)

        t = getattr(agent, "_background_review_thread", None)
        assert t is not None, "the review thread handle must be reachable from the agent"
        assert t.name == "bg-review"

    def test_wait_is_a_noop_when_there_is_no_review(self):
        import run_agent

        agent = object.__new__(run_agent.AIAgent)
        assert agent.wait_for_background_review() is True

    def test_wait_joins_a_running_review_and_is_bounded(self):
        import run_agent

        released = threading.Event()
        agent = object.__new__(run_agent.AIAgent)
        t = threading.Thread(target=released.wait, daemon=True)
        t.start()
        agent._background_review_thread = t
        try:
            assert agent.wait_for_background_review(timeout=0.2) is False, (
                "a review still running must report False so the caller can decide "
                "rather than block a scheduler forever")
            released.set()
            assert agent.wait_for_background_review(timeout=5) is True
        finally:
            released.set()
            t.join(timeout=5)
