"""Per-PC Postgres role provisioning for direct-DB workers (v2).

The server's admin connection (neondb_owner) creates one LOGIN role per
enrolled PC. All worker roles inherit the kanban_worker group role, which
carries the actual grants — future schema changes need one GRANT to the
group, not per-PC surgery. Identifiers are interpolated into SQL text (DDL
can't take bind params); safety comes from the strict ROLE_RE shape and the
token_urlsafe password alphabet, both validated with explicit ValueError raises.
"""
import re
import secrets

from sqlalchemy import text
from sqlalchemy.engine import make_url

GROUP_ROLE = "kanban_worker"
ROLE_RE = re.compile(r"^worker_c\d+_w\d+$")
# token_urlsafe alphabet: A-Za-z0-9_- ; never contains quotes.
PASSWORD_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# INSERT on work_queue is required by the failure-requeue path (worker inserts
# the retry row itself); INSERT on ticket_questions is the blocked-status
# equivalent (worker inserts the question itself when an agent escalates).
# No DELETE/UPDATE on ticket_questions — answering is a human, browser-side
# action through the server's own DB session, never the worker role. UPDATE on
# ticket_chat is the chat pump's delivered_at ack (mark_chat_delivered); a
# human's own chat messages are inserted through the server's own DB session,
# same as ticket_questions answers, so the worker role never needs INSERT
# there. No DELETE anywhere; users/auth_tokens untouched.
GROUP_GRANTS = [
    f"GRANT SELECT ON tickets, boards, clusters, workers, "
    f"work_queue, comments, ticket_deps, ticket_questions, ticket_chat TO {GROUP_ROLE}",
    f"GRANT INSERT ON comments, work_queue, ticket_questions TO {GROUP_ROLE}",
    f"GRANT UPDATE ON work_queue, tickets, workers, ticket_chat TO {GROUP_ROLE}",
    f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {GROUP_ROLE}",
]


def role_name_for(cluster_id: int, worker_id: int) -> str:
    return f"worker_c{cluster_id}_w{worker_id}"


def can_provision(engine) -> bool:
    return engine.url.get_backend_name() == "postgresql"


def ensure_worker_group(engine) -> None:
    """Create the NOLOGIN group role and (re)apply its grants. Idempotent."""
    if not can_provision(engine):
        return
    with engine.begin() as conn:
        conn.execute(text(
            f"DO $$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{GROUP_ROLE}') "
            f"THEN CREATE ROLE {GROUP_ROLE} NOLOGIN; END IF; END $$"
        ))
        for grant in GROUP_GRANTS:
            conn.execute(text(grant))


def provision_role(engine, role_name: str) -> str:
    """(Re)create the per-PC LOGIN role; returns its fresh password.

    Re-enrolling an existing PC rotates the password: old backends are
    terminated and the role recreated.
    """
    if not ROLE_RE.match(role_name):
        raise ValueError(f"unsafe role name: {role_name!r}")
    password = secrets.token_urlsafe(24)
    if not PASSWORD_RE.match(password):
        raise ValueError(f"unsafe password generated: {password!r}")
    with engine.begin() as conn:
        _terminate_backends(conn, role_name)
        conn.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))
        conn.execute(text(f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD '{password}'"))
        conn.execute(text(f'GRANT {GROUP_ROLE} TO "{role_name}"'))
    return password


def revoke_role(engine, role_name: str) -> None:
    if not ROLE_RE.match(role_name):
        raise ValueError(f"unsafe role name: {role_name!r}")
    with engine.begin() as conn:
        _terminate_backends(conn, role_name)
        conn.execute(text(f'DROP ROLE IF EXISTS "{role_name}"'))


def _terminate_backends(conn, role_name: str) -> None:
    """Kick live sessions so DROP ROLE fully cuts access. Best-effort: on
    providers where pg_terminate_backend is restricted, the drop still
    prevents *new* connections."""
    try:
        conn.execute(text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            f"WHERE usename = '{role_name}'"
        ))
    except Exception:
        pass


def build_worker_dsn(admin_url, role_name: str, password: str) -> str:
    """Worker DSN: same host/db as the admin URL, worker credentials, plain
    postgresql:// scheme (psycopg-ready), sslmode=require guaranteed."""
    url = make_url(str(admin_url))
    url = url.set(drivername="postgresql", username=role_name, password=password)
    q = dict(url.query)
    q.setdefault("sslmode", "require")
    url = url.set(query=q)
    return url.render_as_string(hide_password=False)
