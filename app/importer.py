"""Translate tickets from the local file-based .kanban tool into cloud tickets.

The local tool stores one JSON file per ticket in a board folder. Its status
vocabulary is todo/ready/in_progress/blocked/pending/completed; ours is
todo/ready/doing/blocked/done/failed/killed. Local tickets also carry plan and
audit fields we have no column for.

Everything here is pure: no database, no request, no filesystem. The browser
does the file reading (the server cannot see the operator's disk) and posts a
key-whitelisted payload; every decision about what that payload *means* is made
here, where it can be tested.
"""
import datetime

from .models import TICKET_STATUSES, utcnow

# Refuse pathological uploads. The largest live local board is 41 tickets.
MAX_IMPORT_TICKETS = 500

# Local vocabulary -> cloud vocabulary. Statuses already in TICKET_STATUSES pass
# through untouched, so only the genuinely local names need an entry.
#
# A local 'blocked' is blocked on something in the *local* tool that came along
# with none of its context, so it lands in todo rather than in our blocked
# column, which means "a cloud agent or worker needs a human here".
# 'review' is not a local status but was a cloud one until ticket #20 removed
# it, so a board exported before then can still carry it: it maps to done, the
# same way app/db.py's migration rewrote the rows that already had it. Without
# this entry the pass-through below would drop it to todo, silently reopening
# finished work. render_body() always states the original local status, so
# either translation is visible on the imported ticket rather than lost.
STATUS_MAP = {
    "todo": "todo",
    "pending": "todo",
    "blocked": "todo",
    "ready": "ready",
    "in_progress": "doing",
    "completed": "done",
    "review": "done",
}

DEFAULT_BOARD_NAME = "Imported board"

# Local field -> appendix heading, in render order. These are plan content (what
# the ticket intends), unlike history/commitGate/runLogFile, which are run
# exhaust from another machine and are dropped in the browser.
APPENDIX_SECTIONS = [
    ("dependsOn", "Depends on"),
    ("blocks", "Blocks"),
    ("steps", "Steps"),
    ("files", "Files"),
    ("outputs", "Outputs"),
]

# Short lists read better inline; long ones need bullets.
_INLINE_SECTIONS = {"dependsOn", "blocks"}


def map_status(local: object) -> str:
    """Map a local ticket status onto the cloud vocabulary.

    Unknown, missing and non-string statuses become 'todo' rather than raising:
    one odd ticket should not fail a 40-ticket import.
    """
    if not isinstance(local, str):
        return "todo"
    key = local.strip().lower()
    if key in STATUS_MAP:
        return STATUS_MAP[key]
    if key in TICKET_STATUSES:
        return key
    return "todo"


def _section(field: str, heading: str, value: object) -> str:
    """Render one appendix section, or '' when the field is absent or empty."""
    if not isinstance(value, (list, tuple)):
        return ""
    items = [str(v).strip() for v in value if str(v).strip()]
    if not items:
        return ""
    if field in _INLINE_SECTIONS:
        return f"**{heading}:** " + ", ".join(items)
    return f"**{heading}:**\n" + "\n".join(f"- {item}" for item in items)


def render_body(raw: dict, board_slug: str, local_id: object) -> str:
    """The cloud ticket body: the local detail plus a provenance appendix."""
    detail = raw.get("detail")
    detail = detail.strip() if isinstance(detail, str) else ""

    status = raw.get("status")
    status = status.strip() if isinstance(status, str) else "unknown"
    provenance = (
        f"*Imported from local board `{board_slug}` #{local_id} "
        f"(local status: {status or 'unknown'})*"
    )

    parts = [provenance]
    for field, heading in APPENDIX_SECTIONS:
        rendered = _section(field, heading, raw.get(field))
        if rendered:
            parts.append(rendered)

    appendix = "---\n" + "\n\n".join(parts)
    return f"{detail}\n\n{appendix}" if detail else appendix


def _parse_ts(value: object) -> datetime.datetime:
    """Parse a local ISO 8601 timestamp into naive UTC, matching models.utcnow.

    Falls back to now for absent or malformed values — a comment with a bad
    timestamp is still worth keeping.
    """
    if isinstance(value, str):
        try:
            parsed = datetime.datetime.fromisoformat(value.strip())
        except ValueError:
            return utcnow()
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(datetime.timezone.utc).replace(tzinfo=None)
        return parsed
    return utcnow()


def _normalize_comments(raw: object) -> list[dict]:
    """Keep only comments that have both a writer and a message."""
    if not isinstance(raw, list):
        return []
    out = []
    for comment in raw:
        if not isinstance(comment, dict):
            continue
        writer = comment.get("writer")
        message = comment.get("message")
        if not isinstance(writer, str) or not writer.strip():
            continue
        if not isinstance(message, str) or not message.strip():
            continue
        out.append(
            {
                "writer": writer.strip()[:255],
                "message": message,
                "created_at": _parse_ts(comment.get("timestamp")),
            }
        )
    return out


def normalize_ticket(raw: object, board_slug: str, local_id: object) -> dict | None:
    """One local ticket -> {title, body, status, comments}.

    Returns None for anything unusable (not an object, no title) so the caller
    can skip it and carry on with the rest of the board.
    """
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    return {
        "title": title.strip()[:500],
        "body": render_body(raw, board_slug, local_id),
        "status": map_status(raw.get("status")),
        "comments": _normalize_comments(raw.get("comments")),
    }


def unique_board_name(existing: list[str], desired: str) -> str:
    """A board name not already in use, suffixed '(2)', '(3)', ... on collision.

    Compared trimmed and lowercased, the same way default_board() matches names.
    """
    desired = (desired or "").strip() or DEFAULT_BOARD_NAME
    taken = {name.strip().lower() for name in existing}
    if desired.lower() not in taken:
        return desired
    n = 2
    while f"{desired.lower()} ({n})" in taken:
        n += 1
    return f"{desired} ({n})"


def sort_key(local_id: object) -> tuple:
    """Order tickets by local id so cloud ids follow local ones: 2 before 10."""
    text = "" if local_id is None else str(local_id).strip()
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text)
