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
import re

MAX_SLUG_WORDS = 6


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
            "than committing anyway."
        )

    parts.append(
        "\nWhen you are done, reply with a concise summary of what you changed "
        "and why. That summary is posted back to the ticket as a comment, so it "
        "is the only thing a human will see by default."
    )
    return "\n".join(parts)
