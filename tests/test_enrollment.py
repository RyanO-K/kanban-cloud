"""Enrollment module: DSN building and pure helpers (role SQL covered by live smoke)."""
import collections
import re
from pathlib import Path

import pytest

from app import enrollment
from app.db import make_engine
from app.models import Base


def test_role_name_shape():
    assert enrollment.role_name_for(3, 17) == "worker_c3_w17"


def test_can_provision_only_on_postgres(tmp_path):
    sqlite = make_engine(f"sqlite:///{tmp_path / 'e.db'}")
    assert enrollment.can_provision(sqlite) is False
    pg = make_engine("postgresql://u:p@example.invalid/db")  # never connected
    assert enrollment.can_provision(pg) is True


def test_build_worker_dsn_swaps_credentials_and_keeps_host():
    admin = "postgresql+psycopg://neondb_owner:adminpw@ep-x.aws.neon.tech/neondb?sslmode=require"
    dsn = enrollment.build_worker_dsn(admin, "worker_c1_w2", "s3cr3t")
    assert dsn.startswith("postgresql://worker_c1_w2:s3cr3t@ep-x.aws.neon.tech/neondb")
    assert "sslmode=require" in dsn
    assert "adminpw" not in dsn
    assert "+psycopg" not in dsn


def test_build_worker_dsn_adds_sslmode_when_missing():
    admin = "postgresql://u:p@host/db"
    dsn = enrollment.build_worker_dsn(admin, "worker_c1_w1", "pw")
    assert "sslmode=require" in dsn


def test_provision_role_rejects_bad_role_names(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'e.db'}")
    with pytest.raises(ValueError):
        enrollment.provision_role(engine, "worker_c1_w1; DROP TABLE users--")


def test_revoke_role_rejects_bad_role_names(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'e.db'}")
    with pytest.raises(ValueError):
        enrollment.revoke_role(engine, "worker_c1_w1; DROP TABLE users--")


# ---------- GROUP_GRANTS actually covers what the worker does ----------
#
# A missing grant is invisible until it reaches a real Postgres worker role,
# where it takes down every slot on every enrolled PC at once
# (InsufficientPrivilege on the claim). So the required privileges are derived
# from worker.py's own SQL instead of being maintained by hand in two places.

KEYWORD_PRIVILEGE = {
    "FROM": "SELECT", "JOIN": "SELECT", "INSERT INTO": "INSERT",
    "UPDATE": "UPDATE", "DELETE FROM": "DELETE",
}


def worker_privileges():
    """{privilege -> {table}} for every statement in worker.py, restricted to
    real mapped tables so prose in comments ("UPDATE below preserves ...")
    can't masquerade as a table reference."""
    source = (Path(enrollment.__file__).parents[1] / "worker.py").read_text(encoding="utf-8")
    known = set(Base.metadata.tables)
    required = collections.defaultdict(set)
    pattern = r"\b(FROM|JOIN|INSERT INTO|UPDATE|DELETE FROM)\s+([a-z_]+)"
    for keyword, table in re.findall(pattern, source):
        if table in known:
            required[KEYWORD_PRIVILEGE[keyword]].add(table)
    return required


def granted_privileges():
    """{privilege -> {table}} as GROUP_GRANTS actually grants it."""
    granted = collections.defaultdict(set)
    for statement in enrollment.GROUP_GRANTS:
        privileges, _, target = statement[len("GRANT "):].partition(" ON ")
        target = target.rsplit(" TO ", 1)[0]
        if "ALL SEQUENCES" in target:
            continue
        for privilege in (p.strip() for p in privileges.split(",")):
            for table in (t.strip() for t in target.split(",")):
                granted[privilege].add(table)
    return granted


def test_group_grants_cover_every_table_the_worker_touches():
    required, granted = worker_privileges(), granted_privileges()
    missing = {
        privilege: sorted(tables - granted[privilege])
        for privilege, tables in required.items()
        if tables - granted[privilege]
    }
    assert missing == {}, f"worker role would hit InsufficientPrivilege: {missing}"


def test_group_grants_include_profiles_for_the_claim():
    """Regression: agent profiles shipped without a grant, so every slot on
    every PC failed the claim with InsufficientPrivilege."""
    assert "profiles" in granted_privileges()["SELECT"]


def test_cluster_settings_is_updatable_for_its_row_lock():
    """cluster_claim_gate takes `SELECT ... FOR UPDATE` on it, and Postgres
    demands the UPDATE privilege to lock a row even when nothing is written."""
    assert "cluster_settings" in granted_privileges()["UPDATE"]
