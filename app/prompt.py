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

# Appended to the agent's normal summary (not a replacement for it, unlike
# QUESTION_MARKER) when the board has commit_requirements: the agent's own
# verdict on whether it satisfied them. worker.py records this on
# tickets.commit_gate and, when the board has auto_push on, refuses to push a
# branch whose gate says requirements were not met — see parse_commit_gate.
COMMIT_GATE_MARKER = "KANBAN_COMMIT_GATE:"


def slugify(text: str) -> str:
    """A branch-safe slug for a ticket title. Never returns an empty string."""
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return "-".join(words[:MAX_SLUG_WORDS]) or "ticket"


def _filled(board: dict, key: str) -> str:
    """A board field's text, or "" when it is missing or only whitespace."""
    return (board.get(key) or "").strip()


def ticket_branch_name(ticket: dict) -> str:
    """The deterministic branch name an agent creates for one ticket.

    worker.py recomputes this same name after the agent finishes, to push
    whatever branch it just committed — so this must stay the single source
    both sides call.
    """
    return f"{ticket['id']}-{slugify(ticket.get('title', ''))}"


def build_agent_prompt(ticket: dict, board: dict, directory: str) -> str:
    """Compose the full instruction for one ticket.

    Sections whose source field is empty are omitted entirely, so a board with
    no metadata yields a short prompt rather than a form full of blank
    headings.
    """
    branch = ticket_branch_name(ticket)
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
            "than committing anyway.\n"
            "Either way, end your final reply with this line reporting your "
            "verdict — in addition to your normal summary, not instead of it:\n"
            f'{COMMIT_GATE_MARKER} {{"requirements_met": true or false, "summary": '
            '"<one line: what you checked and what you found>"}'
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


def build_resume_prompt(resume: dict) -> str:
    """The short continuation sent when an unblocked ticket resumes its own
    prior Claude CLI session (`claude --resume <session_id>`) instead of
    restarting from build_agent_prompt. The resumed session already has the
    ticket, project context and prior conversation loaded — only the question
    and its answer need repeating.
    """
    parts = [
        "You asked a question and were paused. A human has now answered it:",
        f"\nYour question:\n{resume['question']}",
        f"\nAnswer: {resume['answer_value']}",
    ]
    if (resume.get("answer_notes") or "").strip():
        parts.append(f"\nNotes: {resume['answer_notes']}")
    parts.append(
        "\nContinue the ticket using this answer — do not restart or redo "
        "work already done. If you hit another genuine ambiguity, use the "
        f"same {QUESTION_MARKER} escalation as before. When you are done, "
        "reply with a concise summary of what you changed and why."
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


def parse_commit_gate(text: str) -> dict | None:
    """Pull the agent's self-reported commit-gate verdict out of its captured
    output. Returns None when the marker is absent, the JSON after it is
    malformed, or "requirements_met" is missing/not a bool — any of which
    means the caller cannot treat the requirement as verified, the same as an
    explicit False.

    Unlike QUESTION_MARKER, this marker is appended after a normal summary
    rather than replacing the whole reply, so only the text after the LAST
    occurrence is parsed — same reasoning as parse_question: the guidance
    text itself contains the marker as an example.
    """
    if not text or COMMIT_GATE_MARKER not in text:
        return None
    _, _, tail = text.rpartition(COMMIT_GATE_MARKER)
    try:
        data = json.loads(tail.strip())
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("requirements_met"), bool):
        return None
    return {
        "requirements_met": data["requirements_met"],
        "summary": str(data.get("summary") or "").strip(),
    }
