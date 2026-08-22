"""The prompt handed to a Claude CLI agent working one cloud ticket.

Pure and stdlib-only by contract: ``worker.py`` imports this module, and
``worker.py`` is frozen into a single-file exe by PyInstaller. Importing
SQLAlchemy or FastAPI here would pull the entire server into that exe.

This is the cloud counterpart of ``orchestrator._build_agent_prompt`` in the
local ``.kanban`` tool, trimmed to what this phase supports: there are no
profiles, no prior-question context and no chat backlog yet.

The `directory` argument is a parameter rather than a board field on purpose.
The same board is worked by several machines with different layouts, so the
folder is per-PC state and lives in each worker's own config.
"""
import json
import re

MAX_SLUG_WORDS = 6

# The agent's entire final reply must be this one line when it needs to stop
# and ask a human something — see the escalation guidance appended in
# build_agent_prompt, and parse_question below which reads it back out of the
# executor's captured output.
QUESTION_MARKER = "KANBAN_QUESTION:"
QUESTION_TYPES = ("input", "choice")


def slugify(text: str) -> str:
    """A branch-safe slug for a ticket title. Never returns an empty string."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(words[:MAX_SLUG_WORDS]) or "ticket"


def _filled(board: dict, key: str) -> str:
    """A board field's text, or "" when it is missing or only whitespace."""
    return (board.get(key) or "").strip()


def build_agent_prompt(ticket: dict, board: dict, directory: str) -> str:
    """Compose the full instruction for one ticket.

    Sections whose source field is empty are omitted entirely, so a board with
    no metadata yields a short prompt rather than a form full of blank
    headings.
    """
    branch = f"{ticket['id']}-{slugify(ticket.get('title', ''))}"
    parts = [
        "You are working a single kanban ticket. Keep changes minimal and "
        "focused on what the ticket asks for.",
        f"\nTicket #{ticket['id']}: {ticket.get('title', '')}",
        f"\nDetails:\n{(ticket.get('body') or '').strip() or '(no details provided)'}",
        f"\nWorking directory: {directory}\n"
        f"This is this machine's checkout of the board \"{board.get('name', '')}\". "
        "Everything you do happens here; do not go looking for the project "
        "anywhere else on this PC.",
    ]

    if _filled(board, "description"):
        parts.append(f"\nProject background:\n{_filled(board, 'description')}")
    if _filled(board, "out_of_scope"):
        parts.append(f"\nOut of scope — do not touch:\n{_filled(board, 'out_of_scope')}")

    if board.get("use_worktrees"):
        parts.append(
            "\nGit workflow: isolate your changes in a git worktree.\n"
            f"  git worktree add .claude/worktrees/ticket-{ticket['id']} -b {branch}\n"
            "Check that .claude/worktrees/ is gitignored first. Commit there when "
            "you are done, and do not push — pushing is handled separately."
        )
    else:
        parts.append(
            f"\nGit workflow: create a branch named `{branch}` in the working "
            "directory and commit your changes to it. Do not commit to the "
            "default branch, and do not push — pushing is handled separately."
        )

    if _filled(board, "commit_requirements"):
        parts.append(
            "\nBefore you commit, this project requires:\n"
            f"{_filled(board, 'commit_requirements')}\n"
            "If you cannot satisfy that, stop and say so in your summary rather "
            "than committing anyway."
        )

    parts.append(
        "\nIf you hit a genuine ambiguity you cannot resolve yourself, stop "
        "before making any further changes and reply with ONLY this one line "
        "(nothing before or after it):\n"
        f'{QUESTION_MARKER} {{"question": "<your question>", "type": "input", '
        '"format": null, "options": null, "multi": false}\n'
        'Use "type": "choice" with an "options" array of strings when you\'re '
        'offering a fixed set of choices ("multi": true allows more than one). '
        "The ticket is parked as blocked and a human's answer requeues it — "
        "do not guess and do not fail the ticket over it."
    )
    parts.append(
        "\nWhen you are done, reply with a concise summary of what you changed "
        "and why. That summary is posted back to the ticket as a comment, so it "
        "is the only thing a human will see by default."
    )
    return "\n".join(parts)


def parse_question(text: str) -> dict | None:
    """Pull a structured question out of an agent's captured output.

    Returns None for a normal completion (no marker), a malformed/incomplete
    marker, or a marker with no non-blank "question" text — any of which means
    the caller should treat the run as an ordinary success/failure instead of
    an escalation. Only the text after the LAST marker occurrence is parsed,
    so the guidance text above (which itself contains the marker as an
    example) never self-triggers.
    """
    if not text or QUESTION_MARKER not in text:
        return None
    _, _, tail = text.rpartition(QUESTION_MARKER)
    try:
        data = json.loads(tail.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    question = str(data.get("question") or "").strip()
    if not question:
        return None
    qtype = data.get("type") if data.get("type") in QUESTION_TYPES else "input"
    options = data.get("options") if isinstance(data.get("options"), list) else None
    return {
        "question": question,
        "type": qtype,
        "format": (str(data["format"]).strip() or None) if data.get("format") else None,
        "options": options,
        "multi": bool(data.get("multi")),
    }
