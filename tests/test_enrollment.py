"""Enrollment module: DSN building and pure helpers (role SQL covered by live smoke)."""
import pytest

from app import enrollment
from app.db import make_engine


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
