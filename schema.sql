-- kanban-cloud schema (Postgres / Neon flavor).
-- NOTE: the server auto-creates all tables on startup via SQLAlchemy
-- (Base.metadata.create_all), so running this file is OPTIONAL — it documents
-- the schema and can be used to pre-create tables in Neon by hand.

CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    email         VARCHAR(255) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token      VARCHAR(64) PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS clusters (
    id         SERIAL PRIMARY KEY,
    name       VARCHAR(255) NOT NULL,
    join_code  VARCHAR(16) NOT NULL UNIQUE,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cluster_members (
    id         SERIAL PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id),
    user_id    INTEGER NOT NULL REFERENCES users(id),
    joined_at  TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (cluster_id, user_id)
);

-- Cluster-wide dispatch controls (one row per cluster, created alongside it
-- and backfilled for older clusters by app/db.run_migrations). enabled
-- toggles concurrency_cap without losing the configured number; a NULL cap
-- with enabled=TRUE is treated as "no cap" the same as enabled=FALSE.
-- stop_all_requested blocks every claim outright. Enforced inside the claim
-- transaction itself (worker.cluster_claim_gate), not by a central
-- dispatcher, so it holds across N independent worker PCs.
CREATE TABLE IF NOT EXISTS cluster_settings (
    cluster_id         INTEGER PRIMARY KEY REFERENCES clusters(id),
    enabled            BOOLEAN NOT NULL DEFAULT FALSE,
    concurrency_cap    INTEGER,
    stop_all_requested BOOLEAN NOT NULL DEFAULT FALSE
);

-- A named agent configuration: the tool allowlist, model and system prompt
-- an agent run is launched with. Referenced (by id, not FK-enforced, so a
-- deleted profile just leaves a dangling id) from boards.default_profile_id
-- and tickets.profile_id — see worker.resolve_profile for the fallback rule.
CREATE TABLE IF NOT EXISTS profiles (
    id            SERIAL PRIMARY KEY,
    cluster_id    INTEGER NOT NULL REFERENCES clusters(id),
    name          VARCHAR(255) NOT NULL,
    allowed_tools TEXT NOT NULL,
    model         VARCHAR(128),
    system_prompt TEXT,
    created_at    TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (cluster_id, name)
);

-- description/out_of_scope/commit_requirements/use_worktrees are the project
-- context injected into every agent prompt built for this board. repo_url is
-- the git clone URL a worker with no --set-path entry auto-clones under its
-- own AppData folder. The folder the code actually lives in on a given PC is
-- deliberately NOT here: it is per-PC and lives in each worker's own
-- .worker_config.json (or is derived from repo_url). default_profile_id is
-- this board's fallback agent profile, used when a ticket names none of its
-- own (see tickets.profile_id). auto_push is an opt-in switch (default off):
-- a worker only pushes a finished ticket's branch to origin when this is
-- true, and even then only if the ticket's commit_gate (see tickets below)
-- reports requirements_met — push credentials are the worker PC's own
-- ambient git auth and never reach this server either way.
CREATE TABLE IF NOT EXISTS boards (
    id                  SERIAL PRIMARY KEY,
    cluster_id          INTEGER NOT NULL REFERENCES clusters(id),
    name                VARCHAR(255) NOT NULL,
    description         TEXT,
    out_of_scope        TEXT,
    commit_requirements TEXT,
    use_worktrees       BOOLEAN NOT NULL DEFAULT FALSE,
    repo_url            TEXT,
    default_profile_id  INTEGER,
    auto_push           BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workers (
    id         SERIAL PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id),
    name       VARCHAR(255) NOT NULL,
    role_name  VARCHAR(64),
    revoked    BOOLEAN NOT NULL DEFAULT FALSE,
    status     VARCHAR(32) NOT NULL DEFAULT 'idle',   -- idle | working
    -- Slots this PC runs at once, and how many are busy. Reported by the
    -- worker itself every heartbeat.
    concurrency INTEGER NOT NULL DEFAULT 1,
    running     INTEGER NOT NULL DEFAULT 0,
    -- Website-set concurrency request (ticket #18). NULL = the PC picks its
    -- own limit; when set, the worker honors it ahead of local config.
    desired_concurrency INTEGER,
    last_seen  TIMESTAMP NOT NULL DEFAULT now(),
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (cluster_id, name)
);

-- status: todo | ready | doing | blocked | review | done | failed | killed
-- blocked = the agent raised a question (see ticket_questions) and released
-- its work_queue slot; answering the question auto-requeues it.
-- target_worker NULL = any worker in the cluster may claim.
CREATE TABLE IF NOT EXISTS tickets (
    id              SERIAL PRIMARY KEY,
    board_id        INTEGER NOT NULL REFERENCES boards(id),
    title           VARCHAR(500) NOT NULL,
    body            TEXT NOT NULL DEFAULT '',
    status          VARCHAR(32) NOT NULL DEFAULT 'todo',
    created_by      INTEGER NOT NULL REFERENCES users(id),
    assigned_worker INTEGER REFERENCES workers(id),
    target_worker   INTEGER REFERENCES workers(id),
    -- Per-ticket agent profile override; beats boards.default_profile_id.
    -- NULL falls through to the board, then to the worker's own
    -- --allowed-tools default (worker.resolve_profile).
    profile_id      INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0,
    -- Claude CLI session of the latest attempt: `claude --resume <id>`.
    session_id      VARCHAR(64),
    -- Drag-order rank, ascending. Claimed ahead of queue age by CLAIM_SQL;
    -- ties (the common case — most tickets never get dragged) fall back to
    -- queued_at. Not unique or contiguous: only relative order matters.
    "order"         INTEGER NOT NULL DEFAULT 0,
    -- NULL = not yet triaged. Set once by initial triage (worker.py's
    -- triage_todo_tickets), which promotes the ticket todo -> ready in the
    -- same guarded UPDATE; never overwritten afterward.
    model           VARCHAR(32),
    -- The agent's self-reported verdict on the board's commit_requirements,
    -- JSON-encoded {"requirements_met": bool, "summary": str}. Written by the
    -- worker (worker.py's finish_work) from the KANBAN_COMMIT_GATE: marker in
    -- its captured output (see app/prompt.py's parse_commit_gate); NULL when
    -- the board has no commit_requirements or the agent never reported one.
    commit_gate     TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    updated_at      TIMESTAMP NOT NULL DEFAULT now()
);

-- Dependency edges: ticket_id is not claimable (see the CLAIM_SQL predicate
-- in worker.py) until depends_on_id reaches 'done' or 'review'. No cluster_id
-- of its own — both tickets carry one via their board, checked by the app.
-- `blocks` (the reverse edge) is derived by querying this table by
-- depends_on_id rather than stored.
CREATE TABLE IF NOT EXISTS ticket_deps (
    id             SERIAL PRIMARY KEY,
    ticket_id      INTEGER NOT NULL REFERENCES tickets(id),
    depends_on_id  INTEGER NOT NULL REFERENCES tickets(id),
    UNIQUE (ticket_id, depends_on_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id         SERIAL PRIMARY KEY,
    ticket_id  INTEGER NOT NULL REFERENCES tickets(id),
    writer     VARCHAR(255) NOT NULL,
    message    TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT now()
);

-- Work queue doubles as the assignment log; one row per delegation attempt.
-- Atomic claim: UPDATE work_queue SET status='claimed', claimed_by=$w
--               WHERE id=$id AND status='queued'  (rowcount 0 => lost race).
-- heartbeat_at is set at claim time and refreshed periodically by the
-- claiming slot while it runs (worker.py's _claim_heartbeat_loop). Every
-- worker opportunistically reaps claims whose heartbeat has gone stale
-- (worker.reap_stale_claims): the row -> failed, and (attempts permitting)
-- the ticket -> ready with a fresh queued row, same MAX_ATTEMPTS budget as
-- an explicit failure.
CREATE TABLE IF NOT EXISTS work_queue (
    id           SERIAL PRIMARY KEY,
    ticket_id    INTEGER NOT NULL REFERENCES tickets(id),
    cluster_id   INTEGER NOT NULL REFERENCES clusters(id),
    -- queued | claimed | done | failed | killed | blocked (agent raised a
    -- question; never re-claimed — a fresh row is queued when it's answered)
    status       VARCHAR(32) NOT NULL DEFAULT 'queued',
    claimed_by   INTEGER REFERENCES workers(id),
    -- Owner-requested cancellation of the in-flight claim; the worker polls
    -- this while the agent runs and terminates the child process when set.
    kill_requested BOOLEAN NOT NULL DEFAULT FALSE,
    queued_at    TIMESTAMP NOT NULL DEFAULT now(),
    claimed_at   TIMESTAMP,
    heartbeat_at TIMESTAMP,
    finished_at  TIMESTAMP,
    result       TEXT
);

CREATE INDEX IF NOT EXISTS idx_work_queue_claim
    ON work_queue (cluster_id, status, queued_at);

-- An agent's human-in-the-loop escalation (cloud counterpart of the local
-- tool's orchestrator.question). type: input | choice. options is a
-- JSON-encoded list of strings, meaningful only for type='choice'. Answering
-- (answer_value/answer_notes/answered_at set) is what auto-requeues the
-- ticket — see app/main.py's /api/tickets/{id}/questions/{id}/answer.
CREATE TABLE IF NOT EXISTS ticket_questions (
    id           SERIAL PRIMARY KEY,
    ticket_id    INTEGER NOT NULL REFERENCES tickets(id),
    question     TEXT NOT NULL,
    type         VARCHAR(16) NOT NULL DEFAULT 'input',
    format       VARCHAR(32),
    options      TEXT,
    multi        BOOLEAN NOT NULL DEFAULT FALSE,
    answer_value TEXT,
    answer_notes TEXT,
    created_at   TIMESTAMP NOT NULL DEFAULT now(),
    answered_at  TIMESTAMP
);

-- A human's mid-run message queued for delivery to the agent's live stdin,
-- replacing the local `.kanban` tool's per-run JSONL inbox. delivered_at is
-- set once the worker's chat pump has written the row to the CLI's stdin;
-- unset rows are what a pump run picks up, in id order, whether they were
-- queued before the agent started or typed while it was already running.
CREATE TABLE IF NOT EXISTS ticket_chat (
    id           SERIAL PRIMARY KEY,
    ticket_id    INTEGER NOT NULL REFERENCES tickets(id),
    sender       VARCHAR(255) NOT NULL,
    message      TEXT NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT now(),
    delivered_at TIMESTAMP
);

-- Fine-grained live transcript of one agent run (one row per parsed
-- stream-json turn), replacing the batched-every-4-turns `comments` rows
-- ticket #9 used for anything the UI needs to tail live. work_queue_id scopes
-- rows to one attempt; seq is assigned by the worker and is what a live
-- viewer's ?since_seq= polling tails. The worker also writes the raw stream
-- to a local file per run, but that copy is best-effort/debugging-only — this
-- table is the durable, browser-visible copy. Pruned for long-finished
-- tickets by worker.prune_ticket_log (see worker.py).
CREATE TABLE IF NOT EXISTS ticket_log (
    id            BIGSERIAL PRIMARY KEY,
    ticket_id     INTEGER NOT NULL REFERENCES tickets(id),
    work_queue_id INTEGER REFERENCES work_queue(id),
    seq           INTEGER NOT NULL,
    role          VARCHAR(16) NOT NULL DEFAULT 'assistant',
    text          TEXT NOT NULL,
    created_at    TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ticket_log_ticket_seq
    ON ticket_log (ticket_id, seq);
