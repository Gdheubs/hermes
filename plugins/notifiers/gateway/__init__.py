"""Gateway built-in notifier provider.

This provider wraps the original hardcoded messaging delivery logic of Hermes,
ensuring that notifications route directly to the connected chat platforms
(Telegram, Discord, CLI, etc.) exactly as they did before the NotifierProvider
abstraction was introduced.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from agent.notifier_provider import NotifierProvider
from agent.i18n import t

logger = logging.getLogger(__name__)

class GatewayNotifierProvider(NotifierProvider):
    @property
    def name(self) -> str:
        return "gateway"

    def is_available(self) -> bool:
        return True

    def initialize(self, **kwargs) -> None:
        pass

    async def deliver_kanban_event(
        self,
        events: list[Any],
        subscription: Dict[str, Any],
        task: Any,
        board_slug: str,
        **kwargs
    ) -> bool:
        adapter = kwargs.get("adapter")
        gateway_runner = kwargs.get("gateway_runner")
        sub_key = kwargs.get("sub_key")
        sub_fail_counts = kwargs.get("sub_fail_counts", {})
        MAX_SEND_FAILURES = kwargs.get("max_send_failures", 12)

        if not adapter or not gateway_runner:
            logger.warning("GatewayNotifierProvider requires 'adapter' and 'gateway_runner' in kwargs.")
            return False

        sub = subscription
        plat = adapter.platform if hasattr(adapter, "platform") else None
        platform_str = (sub.get("platform") or "").lower()
        sub_profile = sub.get("notifier_profile") or ""
        title = (task.title if task else sub["task_id"])[:120]
        board_tag = f"[{board_slug}] " if board_slug else ""
        
        mode = sub.get("delivery_mode") or "notify"
        wake_agent = mode in ("notify+wake", "wake")
        send_passive = mode != "wake"
        wake_handoff = ""

        for ev in events:
            kind = ev.kind
            who = (task.assignee if task and task.assignee else None)
            tag = f"@{who} " if who else ""
            if kind == "completed":
                handoff = ""
                payload_summary = None
                if ev.payload and ev.payload.get("summary"):
                    payload_summary = str(ev.payload["summary"])
                if payload_summary:
                    lines = payload_summary.strip().splitlines()
                    h = lines[0][:200] if lines else payload_summary[:200]
                    handoff = f"\n{h}"
                    wake_handoff = h
                elif task and task.result:
                    lines = task.result.strip().splitlines()
                    r = lines[0][:160] if lines else task.result[:160]
                    handoff = f"\n{r}"
                    wake_handoff = r
                msg = (
                    f"✔ {board_tag}{tag}Kanban {sub['task_id']} done"
                    f" — {title}{handoff}"
                )
            elif kind == "blocked":
                reason = ""
                if ev.payload and ev.payload.get("reason"):
                    reason = f": {str(ev.payload['reason'])[:160]}"
                msg = f"⏸ {board_tag}{tag}Kanban {sub['task_id']} blocked{reason}"
            elif kind == "gave_up":
                err = ""
                if ev.payload and ev.payload.get("error"):
                    err = f"\n{str(ev.payload['error'])[:200]}"
                msg = (
                    f"✖ {board_tag}{tag}Kanban {sub['task_id']} gave up "
                    f"after repeated spawn failures{err}"
                )
            elif kind == "crashed":
                msg = (
                    f"✖ {board_tag}{tag}Kanban {sub['task_id']} worker crashed "
                    f"(pid gone); dispatcher will retry"
                )
            elif kind == "timed_out":
                limit = 0
                if ev.payload and ev.payload.get("limit_seconds"):
                    limit = int(ev.payload["limit_seconds"])
                msg = (
                    f"⏱ {board_tag}{tag}Kanban {sub['task_id']} timed out "
                    f"(max_runtime={limit}s); will retry"
                )
            elif kind == "status":
                new_status = ""
                if ev.payload and ev.payload.get("status"):
                    new_status = str(ev.payload["status"])
                msg = f"🔄 {board_tag}{tag}Kanban {sub['task_id']} → {new_status}"
            elif kind == "review_requested":
                handoff = ""
                if ev.payload and ev.payload.get("summary"):
                    handoff = f"\n{str(ev.payload['summary'])[:200]}"
                msg = (
                    f"👀 {board_tag}{tag}Kanban {sub['task_id']} ready for review"
                    f" — {title}{handoff}"
                )
            elif kind == "block_loop_detected":
                reason = ""
                recurrences = None
                if ev.payload:
                    if ev.payload.get("reason"):
                        reason = f": {str(ev.payload['reason'])[:160]}"
                    recurrences = ev.payload.get("recurrences")
                rc = f" (blocked {recurrences}x for the same cause)" if recurrences else ""
                msg = (
                    f"🛑 {board_tag}{tag}Kanban {sub['task_id']} routed to TRIAGE"
                    f" — needs a human decision{rc}{reason}"
                )
            else:
                continue
            
            delivery_metadata = sub.get("delivery_metadata")
            metadata: dict[str, Any] = (
                dict(delivery_metadata)
                if isinstance(delivery_metadata, dict)
                else {}
            )

            if sub.get("thread_id") and not metadata.get("thread_id"):
                metadata["thread_id"] = sub["thread_id"]
                
            from gateway.wake import adapter_supports_push

            if not adapter_supports_push(adapter):
                logger.debug(
                    "kanban notifier: adapter %s has no push "
                    "channel; skipping text ping for %s, relying "
                    "on wake self-post instead",
                    platform_str, sub["task_id"],
                )
                continue
            
            try:
                _send_res = await adapter.send(
                    sub["chat_id"], msg, metadata=metadata,
                )
                if getattr(_send_res, "success", True) is False:
                    raise RuntimeError(
                        "adapter send() reported failure: "
                        f"{getattr(_send_res, 'error', None) or 'unknown error'}"
                    )
                logger.debug(
                    "kanban notifier: delivered %s event for %s to %s/%s on board %s",
                    kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
                )
                
                if kind == "completed":
                    try:
                        await gateway_runner._deliver_kanban_artifacts(
                            adapter=adapter,
                            chat_id=sub["chat_id"],
                            metadata=metadata,
                            event_payload=getattr(ev, "payload", None),
                            task=task,
                        )
                    except Exception as art_exc:
                        logger.debug(
                            "kanban notifier: artifact delivery for %s failed: %s",
                            sub["task_id"], art_exc,
                        )
            except Exception as exc:
                logger.warning(
                    "kanban notifier: send failed for %s on %s: %s",
                    sub["task_id"], platform_str, exc,
                )
                return False

        task_terminal = task and task.status in {"done", "archived"}
        _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")
        _wake_kinds = {ev.kind for ev in events if ev.kind in _WAKE_KINDS}
        from gateway.wake import adapter_supports_push as _adapter_push_ok

        _is_push_adapter = _adapter_push_ok(adapter)
        _session_key = ""
        _synth = ""
        if _wake_kinds:
            _session_key = getattr(task, "session_id", None) or ""
        if _wake_kinds and _session_key:
            _title = (task.title if task else sub["task_id"])[:120]
            _assignee = task.assignee if task else ""
            _parts = []
            if "completed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.completed"))
            if "gave_up" in _wake_kinds: _parts.append(t("gateway.kanban.wake.gave_up"))
            if "crashed" in _wake_kinds: _parts.append(t("gateway.kanban.wake.crashed"))
            if "timed_out" in _wake_kinds: _parts.append(t("gateway.kanban.wake.timed_out"))
            if "blocked" in _wake_kinds: _parts.append(t("gateway.kanban.wake.blocked"))
            _status = t("gateway.kanban.wake.status_joiner").join(_parts) or t("gateway.kanban.wake.status_default")
            _synth = t(
                "gateway.kanban.wake.message",
                task_id=sub["task_id"],
                status=_status,
                title=_title,
                assignee=_assignee,
                board=board_slug,
            )

        if not _is_push_adapter and _wake_kinds and _session_key:
            from gateway.wake import deliver_wake
            try:
                await deliver_wake(
                    adapter,
                    text=_synth,
                    session_id=_session_key,
                )
                logger.info(
                    "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                    sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                )
            except Exception as _wk_err:
                logger.warning(
                    "kanban notifier: wake self-post failed for %s: %s",
                    sub["task_id"], _wk_err, exc_info=True,
                )
                return False

        if _is_push_adapter and _wake_kinds and _session_key:
            try:
                from gateway.session import SessionSource
                from gateway.wake import deliver_wake
                _chat_type = str(sub.get("chat_type") or "").strip()
                if not _chat_type:
                    _delivery_meta = sub.get("delivery_metadata")
                    if isinstance(_delivery_meta, dict):
                        _chat_type = str(
                            _delivery_meta.get("chat_type") or ""
                        ).strip()
                _chat_type = _chat_type or "group"
                _source = SessionSource(
                    platform=plat,
                    chat_id=sub["chat_id"],
                    chat_type=_chat_type,
                    thread_id=sub.get("thread_id") or None,
                    user_id=sub.get("user_id"),
                    profile=sub_profile or None,
                )
                await deliver_wake(
                    adapter,
                    text=_synth,
                    session_id=_session_key,
                    source=_source,
                )
                logger.info(
                    "kanban notifier: woke agent for %s on %s/%s profile=%s events=%s",
                    sub["task_id"], platform_str, sub["chat_id"], sub_profile or "default", _wake_kinds,
                )
            except Exception as _wk_err:
                logger.warning(
                    "kanban notifier: wakeup injection failed for %s: %s",
                    sub["task_id"], _wk_err, exc_info=True,
                )
                
        return True

def register_notifier_provider(ctx):
    ctx.register_notifier_provider(GatewayNotifierProvider())
