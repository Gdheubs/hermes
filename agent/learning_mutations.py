"""User-initiated edit/delete for journey nodes (learned skills + memories).

The journey graph (``agent.learning_graph``) gives every node a stable id:

- **skills** → the skill name (e.g. ``"debugging-hermes-desktop"``)
- **memories** → ``memory:<source>:<index>`` where ``source`` is ``memory``
  (``MEMORY.md``) or ``profile`` (``USER.md``) and ``index`` is the node's
  position in the combined card list (``MEMORY.md`` cards first, then
  ``USER.md``).

This module maps a node id back to its on-disk home and performs the mutation,
shared by the CLI (``hermes journey delete|edit``), the TUI ``/journey`` overlay
(gateway RPCs), and the desktop GUI (REST). Deleting a skill *archives* it
(recoverable via ``hermes curator restore``); deleting a memory rewrites its
file. Pure stdlib + existing skill/memory helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_MEMORY_FILES = {"memory": "MEMORY.md", "profile": "USER.md"}


def parse_node_kind(node_id: str) -> str:
    return "memory" if node_id.startswith("memory:") else "skill"


def _memories_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "memories"


def _parse_memory_id(node_id: str) -> tuple[str, int]:
    """``memory:<source>:<index>`` → (source, global_index)."""
    parts = node_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "memory":
        raise ValueError(f"bad memory node id: {node_id!r}")
    if parts[1] not in _MEMORY_FILES:
        # Nodes contributed by an external memory provider (journey_cards)
        # carry the provider name as their source. They are read-only here:
        # their storage lives in the provider's backend, not in a §-file this
        # module can rewrite.
        raise ValueError(
            f"this memory belongs to the '{parts[1]}' memory provider and is "
            f"read-only in the journey — manage it with the provider's own tools"
        )
    try:
        return parts[1], int(parts[2])
    except ValueError as exc:
        raise ValueError(f"bad memory node id: {node_id!r}") from exc


def _memory_local_index(source: str, global_index: int) -> int:
    """Global card index → position within the source's own file.

    ``_memory_cards`` emits all ``MEMORY.md`` cards before ``USER.md`` cards, so
    a profile card's local index is its global index minus the memory count.
    """
    from agent.learning_graph import _memory_cards

    cards = _memory_cards()
    if not 0 <= global_index < len(cards):
        raise IndexError(f"memory index {global_index} out of range")
    if cards[global_index].get("source") != source:
        raise ValueError("memory node id is stale — refresh the graph")
    if source == "memory":
        return global_index
    return global_index - sum(1 for c in cards if c.get("source") == "memory")


def _locate_memory(source: str, gidx: int) -> tuple[Path, list[str], int]:
    """Resolve a memory card to its file, all §-delimited entries, and local index.

    Entries come from ``MemoryStore._read_file`` — the same parser the memory
    tool uses — so journey indices stay aligned with what the graph renders.
    """
    from tools.memory_tool import MemoryStore

    path = _memories_dir() / _MEMORY_FILES[source]
    if not path.exists():
        raise ValueError(f"{path.name} not found")
    chunks = MemoryStore._read_file(path)
    local = _memory_local_index(source, gidx)
    if not 0 <= local < len(chunks):
        raise ValueError("memory node id is stale — refresh the graph")
    return path, chunks, local


# ── Inspect (edit prefill) ──────────────────────────────────────────────────


def node_detail(node_id: str) -> dict[str, Any]:
    """Current content for an edit prefill. ``content`` is the full SKILL.md
    (skills) or the raw memory chunk (memories)."""
    try:
        return _node_detail(node_id)
    except (ValueError, IndexError) as exc:
        return {"ok": False, "message": str(exc)}


def _node_detail(node_id: str) -> dict[str, Any]:
    if parse_node_kind(node_id) == "memory":
        source, gidx = _parse_memory_id(node_id)
        _, chunks, local = _locate_memory(source, gidx)
        body = chunks[local].strip()

        return {"ok": True, "kind": "memory", "id": node_id, "label": body.splitlines()[0][:80], "content": body}

    from tools.skill_manager_tool import _find_skill

    found = _find_skill(node_id)
    if not found:
        return {"ok": False, "message": f"skill '{node_id}' not found"}
    skill_md = Path(found["path"]) / "SKILL.md"
    if not skill_md.exists():
        return {"ok": False, "message": f"SKILL.md missing for '{node_id}'"}

    return {
        "ok": True,
        "kind": "skill",
        "id": node_id,
        "label": node_id,
        "content": skill_md.read_text(encoding="utf-8"),
    }


# ── Delete ──────────────────────────────────────────────────────────────────


def delete_node(node_id: str) -> dict[str, Any]:
    try:
        return _delete_memory(node_id) if parse_node_kind(node_id) == "memory" else _delete_skill(node_id)
    except (ValueError, IndexError) as exc:
        return {"ok": False, "message": str(exc)}


def _delete_skill(name: str) -> dict[str, Any]:
    from tools import skill_usage

    if skill_usage.get_record(name).get("pinned"):
        return {"ok": False, "message": f"'{name}' is pinned — unpin it first (hermes curator unpin {name})"}

    ok, message = skill_usage.archive_skill(name)
    if ok:
        _clear_skill_cache()

    return {"ok": ok, "message": f"archived '{name}' — restore with: hermes curator restore {name}" if ok else message}


def _delete_memory(node_id: str) -> dict[str, Any]:
    source, gidx = _parse_memory_id(node_id)
    path, chunks, local = _locate_memory(source, gidx)

    del chunks[local]
    _write_memory(path, chunks)

    return {"ok": True, "message": f"deleted memory from {path.name}"}


# ── Edit ────────────────────────────────────────────────────────────────────


def edit_node(node_id: str, content: str) -> dict[str, Any]:
    try:
        return _edit_memory(node_id, content) if parse_node_kind(node_id) == "memory" else _edit_skill(node_id, content)
    except (ValueError, IndexError) as exc:
        return {"ok": False, "message": str(exc)}


def _edit_skill(name: str, content: str) -> dict[str, Any]:
    from tools.skill_manager_tool import _edit_skill as _do_edit

    result = _do_edit(name, content)
    if result.get("success"):
        _clear_skill_cache()

        return {"ok": True, "message": f"updated '{name}'"}

    return {"ok": False, "message": result.get("error", "edit failed")}


def _edit_memory(node_id: str, content: str) -> dict[str, Any]:
    source, gidx = _parse_memory_id(node_id)
    body = content.strip()
    if not body:
        return {"ok": False, "message": "empty memory — use delete to remove it"}
    path, chunks, local = _locate_memory(source, gidx)

    chunks[local] = body
    _write_memory(path, chunks)

    return {"ok": True, "message": f"updated memory in {path.name}"}


# ── Materialize a provider session as a Hermes session ─────────────────────


def build_provider_session_import(
    session_id: str, limit: int = 2000
) -> dict[str, Any]:
    """Shape a provider-side conversation into an ``import_sessions`` payload.

    The journey's "recreate this conversation" action: pulls the raw corpus
    behind a provider-contributed node (``journey_session_messages``) and
    returns a session dict ready for ``SessionDB.import_sessions`` — the same
    validated path the dashboard's session-import uses, so limits, FK safety
    and skip-existing idempotency all apply unchanged.

    Design points:

    - **Stable id** — the Hermes session id IS the provider session id. For
      Hermes-born memories (per-session sync names the Honcho session after
      the Hermes session) this resurrects a deleted conversation under its
      original id; for imported history (``chatgpt-import-…``) the id is
      deterministic, so recreating twice imports once and opens the same
      session thereafter (import skips existing ids).
    - **Role mapping** — providers that know which peer is the human send
      ``role`` per message (the Honcho plugin does); otherwise the first
      message's peer is assumed to be the user. Unattributed messages follow
      the previous turn's role.
    - **Alternation-safe** — consecutive same-role messages are merged so a
      recreated session can be *continued* without violating the strict
      user/assistant alternation contract.
    - **Provenance preserved** — original message timestamps carry over;
      ``started_at`` is the first message's time; ``source`` marks the
      session as journey-recreated without hiding it from session lists.
    """
    sid = str(session_id or "").strip()
    if not sid:
        return {"ok": False, "message": "session_id is required"}

    try:
        from plugins.memory import _get_active_memory_provider, load_memory_provider
    except Exception:
        return {"ok": False, "message": "memory provider framework unavailable"}

    provider_name = _get_active_memory_provider()
    if not provider_name:
        return {"ok": False, "message": "no active memory provider"}
    provider = load_memory_provider(provider_name)
    if provider is None or not hasattr(provider, "journey_session_messages"):
        return {
            "ok": False,
            "message": f"provider '{provider_name}' does not expose session corpora",
        }

    safe_limit = max(1, min(int(limit or 2000), 10_000))
    raw = provider.journey_session_messages(sid, limit=safe_limit) or []

    from agent.learning_graph import _to_int_ts

    shaped: list[dict[str, Any]] = []
    first_peer: str | None = None
    prev_role = "assistant"  # an unattributed opener defaults to user via first_peer
    for m in raw:
        if not isinstance(m, dict):
            continue
        content = str(m.get("content") or "")
        if not content.strip():
            continue
        peer = str(m.get("peer") or "")
        if first_peer is None and peer:
            first_peer = peer
        role = m.get("role")
        if role not in ("user", "assistant"):
            if peer and first_peer:
                role = "user" if peer == first_peer else "assistant"
            else:
                role = "user" if prev_role == "assistant" else "assistant"
        prev_role = role
        ts = _to_int_ts(m.get("timestamp"))
        if shaped and shaped[-1]["role"] == role:
            # Merge consecutive same-role turns (alternation contract).
            shaped[-1]["content"] += "\n\n" + content
            if ts is not None and shaped[-1].get("timestamp") is None:
                shaped[-1]["timestamp"] = ts
        else:
            shaped.append({"role": role, "content": content, "timestamp": ts})

    if not shaped:
        return {
            "ok": False,
            "message": (
                "no source data available — the memory backend is unreachable "
                "or no longer holds this session"
            ),
        }

    timestamps = [m["timestamp"] for m in shaped if m.get("timestamp") is not None]
    started_at = float(min(timestamps)) if timestamps else None

    title = ""
    for m in shaped:
        if m["role"] == "user":
            title = m["content"].strip().splitlines()[0].strip()
            break
    if len(title) > 72:
        title = title[:72].rstrip() + "…"
    if not title:
        title = sid

    session = {
        "id": sid,
        "source": f"journey:{provider_name}",
        "title": title,
        "messages": shaped,
        **({"started_at": started_at} if started_at is not None else {}),
    }
    return {
        "ok": True,
        "provider": provider_name,
        "session": session,
        "message_count": len(shaped),
    }


# ── Helpers ─────────────────────────────────────────────────────────────────


def _write_memory(path: Path, chunks: list[str]) -> None:
    """Atomic temp-file + rename via the memory tool, so a concurrent reader
    never sees a half-written file (and the §-join stays single-sourced)."""
    from tools.memory_tool import MemoryStore

    MemoryStore._write_file(path, [c.strip() for c in chunks if c.strip()])


def _clear_skill_cache() -> None:
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache

        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass
