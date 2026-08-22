"""Initial triage: infer a todo ticket's model and dependencies before it is
promoted to ready. Cloud counterpart of the local `.kanban` tool's
`_real_sonnet_triage` / `validate_triage`.

Pure and stdlib-only by contract, like ``app/prompt.py``: ``worker.py`` imports
this module and is frozen into a single-file exe by PyInstaller, so a
SQLAlchemy import here would pull the entire server into it. See
``docs/superpowers/specs/2026-08-22-triage-design.md`` for where triage runs and
why.
"""
import json

ALLOWED_MODELS = ("haiku", "sonnet", "opus")


def build_triage_prompt(ticket: dict, candidates: list) -> str:
    """Compose the instruction handed to the CLI for one todo ticket.

    `candidates` is every other ticket on the same board — {"id", "title",
    "status"} — offered as the pool triage may pick dependencies from.
    """
    lines = [
        "You are triaging one kanban ticket before it is promoted to the "
        "ready queue. Infer which Claude model tier it needs and which of "
        "the other tickets listed below (if any) it depends on.",
        f"\nTicket #{ticket['id']}: {ticket.get('title', '')}",
        f"\nDetails:\n{(ticket.get('body') or '').strip() or '(no details provided)'}",
    ]
    if candidates:
        lines.append("\nOther tickets on this board (id: title [status]):")
        lines.extend(
            f"  {c['id']}: {c.get('title', '')} [{c.get('status', '')}]"
            for c in candidates
        )
    else:
        lines.append("\nThere are no other tickets on this board.")
    lines.append(
        "\nReply with ONLY one line of JSON (nothing before or after it):\n"
        '{"model": "haiku"|"sonnet"|"opus", "depends_on": [<ticket ids from '
        "the list above that must finish first, or an empty list>]}"
    )
    return "\n".join(lines)


def parse_triage_result(text: str, candidate_ids, ticket_id: int) -> dict | None:
    """Validate a triage reply. Returns {"model", "depends_on"} on success, or
    None for any malformed/invalid reply — a triage failure the caller must
    leave the ticket `todo` for, not guess past.
    """
    if not text:
        return None
    try:
        data = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    model = data.get("model")
    if model not in ALLOWED_MODELS:
        return None
    depends_on = data.get("depends_on")
    if depends_on is None:
        depends_on = []
    if not isinstance(depends_on, list):
        return None
    valid_ids = set(candidate_ids)
    deps = []
    seen = set()
    for dep_id in depends_on:
        if not isinstance(dep_id, int) or isinstance(dep_id, bool):
            return None
        if dep_id == ticket_id or dep_id not in valid_ids:
            return None
        if dep_id not in seen:
            seen.add(dep_id)
            deps.append(dep_id)
    return {"model": model, "depends_on": deps}
