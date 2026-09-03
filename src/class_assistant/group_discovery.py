"""Read-only discovery of WeChat group metadata."""

from __future__ import annotations

from typing import Any


def discover_groups(client: Any) -> list[dict[str, Any]]:
    """Return stable metadata for chatroom sessions without reading messages."""
    try:
        sessions = client.get_sessions()
        result = []
        for session in sessions or []:
            chat_id = str(session.get("username", "") or "")
            if not chat_id.endswith("@chatroom"):
                continue
            display_name = str(
                session.get("displayName") or session.get("displayname")
                or session.get("nickname") or session.get("display_name") or chat_id
            ).strip() or chat_id
            members = client.get_group_members(chat_id)
            result.append({
                "chat_id": chat_id,
                "display_name": display_name,
                "member_count": len(members or []),
            })
        return sorted(result, key=lambda item: (item["display_name"].casefold(), item["chat_id"]))
    except Exception as exc:
        raise RuntimeError(f"group discovery failed: {exc}") from exc
