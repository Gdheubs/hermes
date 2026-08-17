# Copilot ACP Premature Stop Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the default Copilot ACP path recover twice when a tool-capable turn ends with a short action acknowledgement instead of a structured tool call.

**Architecture:** Reuse Hermes' existing intent-ack detector and bounded continuation loop. Extend automatic mode resolution to `copilot-acp`, and mark both recovery messages as ephemeral so they cannot enter durable session history; do not parse or execute prose commands.

**Tech Stack:** Python 3.11+, pytest, Hermes chat-completions conversation loop.

## Global Constraints

- Apply only to `copilot-acp` and existing `codex_responses` automatic behavior.
- Keep the existing two-continuation cap.
- Never execute textual command/XML/JSON representations.
- Preserve strict assistant/user alternation and prompt-prefix stability.
- Do not persist recovery-only transcript scaffolding.

---

### Task 1: Enable automatic intent-ack recovery for Copilot ACP

**Files:**
- Modify: `agent/agent_runtime_helpers.py:3612`
- Test: `tests/agent/test_intent_ack_continuation.py`

**Interfaces:**
- Consumes: agent fields `provider`, `api_mode`, `model`, and `_intent_ack_continuation`.
- Produces: `intent_ack_continuation_mode(agent) -> Literal["off", "codex_only", "all"]`; automatic Copilot ACP resolves to `"all"`.

- [ ] **Step 1: Write the failing provider-mode tests**

Update the test helper to accept a provider and add these assertions:

```python
def _agent(mode="auto", api_mode="chat_completions", model="gpt-5.6-sol", provider="openrouter"):
    return SimpleNamespace(
        _intent_ack_continuation=mode,
        api_mode=api_mode,
        model=model,
        provider=provider,
        _strip_think_blocks=lambda content: content,
    )


def test_auto_enables_copilot_acp_for_action_acknowledgements():
    agent = _agent(provider="copilot-acp")
    assert intent_ack_continuation_mode(agent) == "all"
    assert intent_ack_continuation_enabled(agent) is True


def test_explicit_off_overrides_copilot_acp_default():
    agent = _agent(mode=False, provider="copilot-acp")
    assert intent_ack_continuation_mode(agent) == "off"
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/agent/test_intent_ack_continuation.py -q`

Expected: `test_auto_enables_copilot_acp_for_action_acknowledgements` fails because automatic chat-completions mode currently returns `"off"`.

- [ ] **Step 3: Implement the smallest mode-resolution change**

Change the automatic fallback in `intent_ack_continuation_mode` to preserve existing Codex behavior and opt Copilot ACP into the general detector:

```python
    if getattr(agent, "provider", "") == "copilot-acp":
        return "all"
    return "codex_only" if agent.api_mode == "codex_responses" else "off"
```

Update the function docstring so `auto` explicitly means Codex Responses workspace acknowledgements plus Copilot ACP general action acknowledgements.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/agent/test_intent_ack_continuation.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add agent/agent_runtime_helpers.py tests/agent/test_intent_ack_continuation.py
git commit -m "fix(agent): recover Copilot ACP intent acknowledgements"
```

### Task 2: Keep Copilot ACP recovery scaffolding out of session history

**Files:**
- Modify: `agent/conversation_loop.py:7338`
- Modify: `run_agent.py:230`
- Test: `tests/agent/test_intent_ack_continuation.py`

**Interfaces:**
- Consumes: the existing `_CODEX_ACK_CONTINUATION_NUDGE` retry pair.
- Produces: `_intent_ack_continuation_nudge=True` metadata recognized by `run_agent._is_ephemeral_scaffolding(message) -> bool`.

- [ ] **Step 1: Write the failing persistence-boundary test**

Add:

```python
def test_intent_ack_retry_pair_is_ephemeral_scaffolding():
    from run_agent import _is_ephemeral_scaffolding

    assistant = {
        "role": "assistant",
        "content": "Let me inspect that now.",
        "_intent_ack_continuation_nudge": True,
    }
    user = {
        "role": "user",
        "content": "continue with the real tool call",
        "_intent_ack_continuation_nudge": True,
    }
    assert _is_ephemeral_scaffolding(assistant)
    assert _is_ephemeral_scaffolding(user)
```

- [ ] **Step 2: Run the persistence test and verify RED**

Run: `python -m pytest tests/agent/test_intent_ack_continuation.py::test_intent_ack_retry_pair_is_ephemeral_scaffolding -q`

Expected: FAIL because `_intent_ack_continuation_nudge` is not in `_EPHEMERAL_SCAFFOLDING_FLAGS`.

- [ ] **Step 3: Add the persistence flag and stamp both retry messages**

Add `"_intent_ack_continuation_nudge"` to `_EPHEMERAL_SCAFFOLDING_FLAGS` in `run_agent.py`. In the continuation branch, stamp both messages:

```python
                    interim_msg["_intent_ack_continuation_nudge"] = True
                    messages.append(interim_msg)
                    agent._emit_interim_assistant_message(interim_msg)

                    continue_msg = {
                        "role": "user",
                        "content": _CODEX_ACK_CONTINUATION_NUDGE,
                        "_intent_ack_continuation_nudge": True,
                    }
```

Do not change the nudge text, retry counter, detector, or tool execution path.

- [ ] **Step 4: Run focused persistence and continuation tests**

Run: `python -m pytest tests/agent/test_intent_ack_continuation.py tests/run_agent/test_verification_continuation_budget.py -q`

Expected: all tests pass, including the existing two-retry budget assertions.

- [ ] **Step 5: Commit Task 2**

```bash
git add agent/conversation_loop.py run_agent.py tests/agent/test_intent_ack_continuation.py
git commit -m "fix(agent): keep intent-ack retries ephemeral"
```

### Task 3: Verify the complete Copilot ACP recovery boundary

**Files:**
- Verify: `agent/agent_runtime_helpers.py`
- Verify: `agent/conversation_loop.py`
- Verify: `run_agent.py`
- Verify: `tests/agent/test_intent_ack_continuation.py`
- Verify: `tests/agent/test_copilot_acp_client.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: evidence that automatic provider gating, bounded continuation, ACP parsing, and persistence filtering remain compatible.

- [ ] **Step 1: Run the complete focused suite**

Run:

```bash
python -m pytest \
  tests/agent/test_intent_ack_continuation.py \
  tests/agent/test_copilot_acp_client.py \
  tests/run_agent/test_verification_continuation_budget.py \
  tests/run_agent/test_dropped_tool_call_recovery.py -q
```

Expected: all tests pass with zero failures.

- [ ] **Step 2: Run repository formatting/static checks for changed Python files**

Run: `git diff --check upstream/main...HEAD`

Expected: no output and exit code 0.

- [ ] **Step 3: Inspect the final diff against the design**

Run: `git diff --stat upstream/main...HEAD && git diff upstream/main...HEAD -- agent/agent_runtime_helpers.py agent/conversation_loop.py run_agent.py tests/agent/test_intent_ack_continuation.py`

Expected: only provider gating, ephemeral metadata, and their regression tests; no prose-command execution or unrelated refactoring.

- [ ] **Step 4: Request code review and address every Critical/Important finding**

Use `superpowers:requesting-code-review` with base `upstream/main` and the current `HEAD`. Re-run Step 1 after any review-driven edit.

- [ ] **Step 5: Push the branch to the fork**

Run: `git push -u origin fix/copilot-acp-premature-stop`

Expected: branch is available at `LeePepe/hermes-agent` without modifying upstream.
