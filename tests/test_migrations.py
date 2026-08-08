"""Schema v2: workers get role_name/revoked, lose token."""
import sqlalchemy as sa

from app.db import make_engine, run_migrations
from app.models import Base, Worker


def test_worker_model_has_v2_columns_and_no_token():
    cols = {c.name for c in Worker.__table__.columns}
    assert "role_name" in cols
    assert "revoked" in cols
    assert "token" not in cols


def test_run_migrations_noop_on_sqlite(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    Base.metadata.create_all(engine)
    run_migrations(engine)  # must not raise
    insp = sa.inspect(engine)
    assert "workers" in insp.get_table_names()
