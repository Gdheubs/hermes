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

    async def deliver_kanban_events(
        self,
        events: list[Any],
        subscription: Dict[str, Any],
        task: Any,
        board_slug: str,
        **kwargs
    ) -> bool:
        adapter = kwargs.get("adapter")
        if not adapter:
            logger.warning("GatewayNotifierProvider requires an 'adapter' in kwargs.")
            return False

        sub = subscription
        plat = adapter.platform if hasattr(adapter, "platform") else None
        platform_str = (sub.get("platform") or "").lower()
        sub_profile = sub.get("notifier_profile") or ""
        title = (task.title if task else sub["task_id"])[:120]
        board_tag = f"[{board_slug}] " if board_slug else ""
        
        # d object mapping to mimic watcher loop
        class D: pass
        d = D()
        d.events = events
        d = {"events": events}

mode = sub.get("delivery_mode") or "notify"
wake_agent = mode in ("notify+wake", "wake")
send_passive = mode != "wake"
# Worker handoff carried into the synthetic wake turn below
# (#70752): without it the woken creator only sees
# "Task X completed" and re-decomposes work that already
# exists on the board.
wake_handoff = ""
for ev in events:
    kind = ev.kind
    # Identity prefix: attribute terminal pings to the
    # worker that did the work. Makes fleets (where one
    # chat subscribes to many tasks) legible at a glance.
    who = (task.assignee if task and task.assignee else None)
    tag = f"@{who} " if who else ""
    if kind == "completed":
        # Prefer the run's summary (the worker's
        # intentional human-facing handoff, carried
        # in the event payload), then fall back to
        # task.result for legacy rows written before
        # runs shipped.
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
            f"Γ£ö {board_tag}{tag}Kanban {sub['task_id']} done"
            f" ΓÇö {title}{handoff}"
        )
    elif kind == "blocked":
        reason = ""
        if ev.payload and ev.payload.get("reason"):
            reason = f": {str(ev.payload['reason'])[:160]}"
        msg = f"ΓÅ╕ {board_tag}{tag}Kanban {sub['task_id']} blocked{reason}"
    elif kind == "gave_up":
        err = ""
        if ev.payload and ev.payload.get("error"):
            err = f"\n{str(ev.payload['error'])[:200]}"
        msg = (
            f"Γ£û {board_tag}{tag}Kanban {sub['task_id']} gave up "
            f"after repeated spawn failures{err}"
        )
    elif kind == "crashed":
        msg = (
            f"Γ£û {board_tag}{tag}Kanban {sub['task_id']} worker crashed "
            f"(pid gone); dispatcher will retry"
        )
    elif kind == "timed_out":
        limit = 0
        if ev.payload and ev.payload.get("limit_seconds"):
            limit = int(ev.payload["limit_seconds"])
        msg = (
            f"ΓÅ▒ {board_tag}{tag}Kanban {sub['task_id']} timed out "
            f"(max_runtime={limit}s); will retry"
        )
    elif kind == "status":
        new_status = ""
        if ev.payload and ev.payload.get("status"):
            new_status = str(ev.payload["status"])
        msg = f"≡ƒöä {board_tag}{tag}Kanban {sub['task_id']} ΓåÆ {new_status}"
    elif kind == "review_requested":
        # Implementation complete; task moved to the
        # first-class review lane. Wake the origin thread.
        handoff = ""
        if ev.payload and ev.payload.get("summary"):
            handoff = f"\n{str(ev.payload['summary'])[:200]}"
        msg = (
            f"≡ƒæÇ {board_tag}{tag}Kanban {sub['task_id']} ready for review"
            f" ΓÇö {title}{handoff}"
        )
    elif kind == "block_loop_detected":
        # A task re-blocked for the same cause past the
        # recurrence limit and was routed to `triage` for a
        # human decision. This is the ONE transition that
        # exists to force human attention, yet it emits no
        # `blocked`/`status` event ΓÇö so before adding it to
        # TERMINAL_KINDS it produced zero notification and
        # the task stalled in triage silently. Ping loudly.
        reason = ""
        recurrences = None
        if ev.payload:
            if ev.payload.get("reason"):
                reason = f": {str(ev.payload['reason'])[:160]}"
            recurrences = ev.payload.get("recurrences")
        rc = f" (blocked {recurrences}x for the same cause)" if recurrences else ""
        msg = (
            f"≡ƒ¢æ {board_tag}{tag}Kanban {sub['task_id']} routed to TRIAGE"
            f" ΓÇö needs a human decision{rc}{reason}"
        )
    else:
        # archived / unblocked are claimed by TERMINAL_KINDS
        # (so the cursor advances past them and they can't
        # wedge a later completed/blocked event behind an
        # unclaimed row) but are intentionally SILENT: an
        # archive needs no user ping, and unblocked is an
        # internal transition. They are also excluded from
        # _WAKE_KINDS below, so they never wake the creator.
        continue
    delivery_metadata = sub.get("delivery_metadata")
    metadata: dict[str, Any] = (
        dict(delivery_metadata)
        if isinstance(delivery_metadata, dict)
        else {}
    )

    if sub.get("thread_id") and not metadata.get("thread_id"):
        metadata["thread_id"] = sub["thread_id"]
    # Adapters with no push channel (the API server ΓÇö
    # ``supports_async_delivery = False``) can NEVER
    # satisfy a text-send: ``send()`` always reports
    # SendResult(success=False) by design (see
    # ApiServerAdapter.send()). Treating that as a
    # delivery failure would rewind/drop the subscription
    # forever and ΓÇö because the wake dispatch below lives
    # in this loop's ``else`` clause ΓÇö would also make the
    # wake-on-completion path (the actual fix for the
    # api_server wrong-session bug) unreachable. So for
    # non-push adapters, skip the doomed send attempt
    # entirely: there is nothing to text-notify, the
    # creator is woken via the self-post below instead.
    from gateway.wake import adapter_supports_push

    if not adapter_supports_push(adapter) and wake_agent:
        logger.debug(
            "kanban notifier: adapter %s has no push "
            "channel; skipping text ping for %s, relying "
            "on wake self-post instead",
            platform_str, sub["task_id"],
        )
        # Do NOT reset the failure counter here: on this
        # path the wake self-post below IS the delivery,
        # so the counter is resolved (reset or bumped) by
        # the self-post outcome, not by skipping the send.
        continue
    if not send_passive:
        # Wake-only subscriptions intentionally skip the
        # visible platform message. The retained wake path
        # below is the sole delivery ΓÇö the failure counter
        # is resolved (reset or bumped) by the wake
        # outcome there, not by skipping the send here.
        continue
    try:
        _send_res = await adapter.send(
            sub["chat_id"], msg, metadata=metadata,
        )
        # A SendResult(success=False) without an exception
        # (returned by push-capable adapters on a genuine
        # transient failure) must count as a FAILED
        # delivery ΓÇö otherwise the cursor advances and the
        # event is permanently lost. Adapters returning
        # None (or anything non-SendResult shaped) keep
        # the legacy "no exception == delivered" contract.
        if getattr(_send_res, "success", True) is False:
            raise RuntimeError(
                "adapter send() reported failure: "
                f"{getattr(_send_res, 'error', None) or 'unknown error'}"
            )
        logger.debug(
            "kanban notifier: delivered %s event for %s to %s/%s on board %s",
            kind, sub["task_id"], platform_str, sub["chat_id"], board_slug,
        )
        # After delivering the text notification, surface
        # any artifact paths the worker referenced in
        # ``kanban_complete(summary=..., artifacts=[...])``
        # (or the legacy ``result`` field) as native
        # uploads. ``extract_local_files`` finds bare
        # absolute paths in the summary;
        # ``send_document`` / ``send_image_file`` uploads
        # them. Only fires on the ``completed`` event so
        # we never spam attachments on retries.
        if kind == "completed":
            try:
                await kwargs.get("gateway_runner")._deliver_kanban_artifacts(
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
        # Reset the failure counter on success.
    except Exception as exc:
            raise _wk_err

    async def _push_wake() -> None:
        """Wake the creator session behind a push adapter.

        Shared by the wake-only (pre-advance, delivery)
        and notify+wake (post-advance, best-effort)
        branches below; raises on failure so the caller
        decides whether to rewind or merely log.
        """
        from gateway.session import SessionSource\nfrom gateway.config import Platform
        from gateway.wake import deliver_wake
        # Rebuild the creator's real session scope from
        # the chat_type persisted on the subscription
        # row (#56580). build_session_key() keys DMs
        # (":dm:<chat_id>") on a wholly different shape
        # from group/thread, so the old hardcoded
        # "group" mis-routed DM/thread creators into a
        # fresh session. Legacy rows written before the
        # column existed may still carry chat_type in
        # delivery_metadata (#60600 rows) ΓÇö fall back
        # to that, then to "group" (the historical
        # default that suits the dashboard/group flows).
        # handle_message() get_or_create_session's the
        # target, so a mismatch only ever degrades to a
        # fresh session, never an exception.
        _chat_type = str(sub.get("chat_type") or "").strip()
        if not _chat_type:
            _delivery_meta = sub.get("delivery_metadata")
            if isinstance(_delivery_meta, dict):
                _chat_type = str(
                    _delivery_meta.get("chat_type") or ""
                ).strip()
        _chat_type = _chat_type or "group"
        _source = SessionSource(
            platform=Platform(platform_str),
            chat_id=sub["chat_id"],
            chat_type=_chat_type,
            thread_id=sub.get("thread_id") or None,
            user_id=sub.get("user_id"),
            user_id_alt=sub.get("user_id_alt"),
            profile=sub_profile or None,
            scope_id=None,
        )
        # deliver_wake preserves the synthetic
        # MessageEvent/handle_message path for
        # push-capable adapters (the non-push /
        # self-post branch is handled BEFORE the
        # cursor advance above).
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

    if _is_push_adapter and not send_passive and _wake_kinds:
        # Wake-only (delivery_mode='wake') push sub: the
        # text ping was intentionally skipped above, so
        # the wake IS the sole delivery. It must succeed
        # BEFORE the cursor advances ΓÇö advancing first
        # would let a failed wake (previously swallowed
        # by the best-effort except below) permanently
        # lose the event. Mirrors the non-push
        # (api_server) self-post ordering above.
        try:
            await _push_wake()
        except Exception as _wk_err:
            raise _wk_err

    # Delivery complete (text ping for push adapters, wake
    # self-post for non-push, wake injection for wake-only
    # push subs): advance cursor. The cursor is the dedup
    # mechanism ΓÇö it prevents re-delivery of the same
    # event on subsequent ticks.
    await asyncio.to_thread(
        self._kanban_advance, sub, d["cursor"], board_slug,
    )
    if not _is_push_adapter:
        # Nothing left to deliver on this path (the wake,
        # if any, already succeeded above).
    # Unsubscribe only on archive. Completion (``done``)
    # remains reversible: controllers reopen completed
    # work for review corrections and continuation. The
    # retained cursor prevents replay while preserving the
    # original delivery and wake ownership for that cycle.
    if _is_push_adapter and send_passive and _wake_kinds:
        # notify+wake: the text ping above was the
        # delivery and the cursor has advanced; the wake
        # injection stays best-effort.
        try:
            await _push_wake()
        except Exception as _wk_err:
            # Best-effort: the notification itself already
            # delivered and the cursor has advanced, so a
            # broken wake path must not wedge the tick ΓÇö but
            # log at WARNING with a traceback rather than
            # DEBUG so a persistently-failing wake is visible
            # in normal logs instead of silently no-op'ing.
            logger.warning(
                "kanban notifier: wakeup injection failed for %s: %s",
                sub["task_id"], _wk_err, exc_info=True,
            )

        return True

def register_notifier_provider(ctx):
    ctx.register_notifier_provider(GatewayNotifierProvider())
