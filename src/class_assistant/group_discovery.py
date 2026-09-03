"""Read-only discovery of WeChat group metadata."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def discover_groups(client: Any) -> list[dict[str, Any]]:
    """Return stable metadata for chatroom sessions without reading messages."""
    try:
        sessions = client.get_sessions()
        result = []
        seen_chat_ids: set[str] = set()
        for session in sessions or []:
            chat_id = str(session.get("username", "") or "")
            if not chat_id.endswith("@chatroom"):
                continue
            if chat_id in seen_chat_ids:
                continue
            seen_chat_ids.add(chat_id)
            display_name = str(
                session.get("displayName") or session.get("displayname")
                or session.get("nickname") or session.get("display_name") or chat_id
            ).strip() or chat_id
            try:
                members = client.get_group_members(chat_id)
                member_count = len(members or [])
            except Exception:
                # A stale/deleted group must not prevent discovery of all
                # other groups.  Do not expose backend errors to logs or UI.
                member_count = 0
                logger.warning(
                    "Group member metadata unavailable; using member_count=%d",
                    member_count,
                )
            result.append({
                "chat_id": chat_id,
                "display_name": display_name,
                "member_count": member_count,
            })
        return sorted(result, key=lambda item: (item["display_name"].casefold(), item["chat_id"]))
    except Exception:
        # Keep logs useful for operations without retaining exception text,
        # tracebacks, paths, or backend-specific data.
        logger.error("Group metadata discovery unavailable")
        raise RuntimeError("group discovery unavailable") from None
