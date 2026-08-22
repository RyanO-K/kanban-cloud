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

-- description/out_of_scope/commit_requirements/use_worktrees are the project
-- context injected into every agent prompt built for this board. repo_url is
-- the git clone URL a worker with no --set-path entry auto-clones under its
-- own AppData folder. The folder the code actually lives in on a given PC is
-- deliberately NOT here: it is per-PC and lives in each worker's own
-- .worker_config.json (or is derived from repo_url).
CREATE TABLE IF NOT EXISTS boards (
    id                  SERIAL PRIMARY KEY,
    cluster_id          INTEGER NOT NULL REFERENCES clusters(id),
    name                VARCHAR(255) NOT NULL,
    description         TEXT,
    out_of_scope        TEXT,
    commit_requirements TEXT,
    use_worktrees       BOOLEAN NOT NULL DEFAULT FALSE,
    repo_url            TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS workers (
    id         SERIAL PRIMARY KEY,
    cluster_id INTEGER NOT NULL REFERENCES clusters(id),
    name       VARCHAR(255) NOT NULL,
    role_name  VARCHAR(64),
    revoked    BOOLEAN NOT NULL DEFAULT FALSE,
    status     VARCHAR(32) NOT NULL DEFAULT 'idle',   -- idle | working
    -- Slots this PC runs at once, and how many are busy. Set by the worker
    -- itself; the server only displays them.
    concurrency INTEGER NOT NULL DEFAULT 1,
    running     INTEGER NOT NULL DEFAULT 0,
    last_seen  TIMESTAMP NOT NULL DEFAULT now(),
    created_at TIMESTAMP NOT NULL DEFAULT now(),
    UNIQUE (cluster_id, name)
);

-- status: todo | ready | doing | review | done | failed
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
    attempts        INTEGER NOT NULL DEFAULT 0,
    -- Claude CLI session of the latest attempt: `claude --resume <id>`.
    session_id      VARCHAR(64),
    -- Drag-order rank, ascending. Claimed ahead of queue age by CLAIM_SQL;
    -- ties (the common case — most tickets never get dragged) fall back to
    -- queued_at. Not unique or contiguous: only relative order matters.
    "order"         INTEGER NOT NULL DEFAULT 0,
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
    status       VARCHAR(32) NOT NULL DEFAULT 'queued',  -- queued | claimed | done | failed
    claimed_by   INTEGER REFERENCES workers(id),
    queued_at    TIMESTAMP NOT NULL DEFAULT now(),
    claimed_at   TIMESTAMP,
    heartbeat_at TIMESTAMP,
    finished_at  TIMESTAMP,
    result       TEXT
);

CREATE INDEX IF NOT EXISTS idx_work_queue_claim
    ON work_queue (cluster_id, status, queued_at);
