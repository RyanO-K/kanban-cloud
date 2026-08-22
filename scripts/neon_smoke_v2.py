"""Live v2 smoke against the real Neon DB. Creates ONLY scratch fixtures and
deletes exactly those rows (plus drops the scratch roles) at the end — never
wipes tables (Ryan's real cluster may exist).

Usage: python scripts/neon_smoke_v2.py "<admin DATABASE_URL>"
Exit 0 = PASS.
"""
import concurrent.futures
import sys
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import worker as worker_mod  # noqa: E402
from app import enrollment  # noqa: E402
from app.main import create_app  # noqa: E402

RACE_N = 8


def main() -> int:
    admin_url = sys.argv[1]
    app = create_app(admin_url, proxy_secret="")
    client = TestClient(app)
    created = {}
    try:
        # -- fixtures via the human API; tolerant of reruns --
        r = client.post("/api/register", json={"email": "smoke-v2@example.com",
                                               "password": "smokepass"})
        if r.status_code == 409:
            # rerun: user exists, fall back to login
            r = client.post("/api/login", json={"email": "smoke-v2@example.com",
                                                "password": "smokepass"})
            assert r.status_code == 200, f"login failed: {r.text}"
        else:
            assert r.status_code == 200, r.text
        headers = {"Authorization": f"Bearer {r.json()['token']}"}

        # discover or create cluster
        clusters = client.get("/api/clusters", headers=headers).json()
        smoke_cluster = next((c for c in clusters if c["name"] == "smoke-v2-cluster"), None)
        if smoke_cluster:
            c = smoke_cluster
        else:
            c = client.post("/api/clusters", json={"name": "smoke-v2-cluster"},
                            headers=headers).json()
        created["cluster_id"] = c["id"]
        board = client.get(f"/api/clusters/{c['id']}/boards", headers=headers).json()[0]
        t = client.post(f"/api/boards/{board['id']}/tickets",
                        json={"title": "smoke v2 ticket", "status": "ready"},
                        headers=headers).json()

        # -- enroll two scratch workers --
        dsns, wids = [], []
        for name in ("smoke-pc-a", "smoke-pc-b"):
            e = client.post("/api/workers/enroll",
                            json={"join_code": c["join_code"], "name": name})
            assert e.status_code == 200, e.text
            dsns.append(e.json()["dsn"])
            wids.append(e.json()["worker_id"])
        print("enrolled 2 workers; roles live")

        # -- N-way claim race on worker A's DSN: exactly one winner --
        def try_claim(_):
            with psycopg.connect(dsns[0], connect_timeout=15) as conn:
                return worker_mod.claim_next(conn, wids[0], c["id"])
        with concurrent.futures.ThreadPoolExecutor(RACE_N) as ex:
            results = list(ex.map(try_claim, range(RACE_N)))
        wins = [r for r in results if r]
        assert len(wins) == 1, f"expected exactly 1 winner, got {len(wins)}"
        work = wins[0]
        print(f"claim race: 1/{RACE_N} winner (assignment {work['assignment_id']})")

        # -- progress comment via direct SQL --
        with psycopg.connect(dsns[0], connect_timeout=15) as conn:
            worker_mod.add_progress(conn, wids[0], "smoke-pc-a", t["id"], "smoke progress 50%")
        print("progress comment inserted")

        # -- finish -> review --
        with psycopg.connect(dsns[0], connect_timeout=15) as conn:
            status = worker_mod.finish_work(conn, wids[0], "smoke-pc-a",
                                            work["assignment_id"], t["id"],
                                            True, "smoke result")
        assert status == "review", status
        print("result recorded; ticket -> review")

        # -- worker role must NOT see auth tables --
        with psycopg.connect(dsns[0]) as conn, conn.cursor() as cur:
            try:
                cur.execute("SELECT count(*) FROM users")
                raise AssertionError("worker role can read users table!")
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
        print("grants: users table correctly denied")

        # -- revoke worker B; its DSN must stop connecting --
        rv = client.post(f"/api/workers/{wids[1]}/revoke", headers=headers)
        assert rv.status_code == 200, rv.text
        try:
            psycopg.connect(dsns[1], connect_timeout=15).close()
            raise AssertionError("revoked worker can still connect!")
        except psycopg.OperationalError:
            pass
        print("revocation: dropped role can no longer connect")
        print("SMOKE PASS")
        return 0
    finally:
        # -- precise cleanup: scratch rows + scratch roles only --
        try:
            with psycopg.connect(
                admin_url.replace("postgresql+psycopg://", "postgresql://"),
                connect_timeout=15, autocommit=True
            ) as conn, conn.cursor() as cur:
                cid = created.get("cluster_id")
                if cid is not None:
                    try:
                        cur.execute("DELETE FROM comments WHERE ticket_id IN "
                                    "(SELECT t.id FROM tickets t JOIN boards b ON b.id=t.board_id "
                                    "WHERE b.cluster_id=%s)", (cid,))
                    except Exception as e:
                        print(f"cleanup: delete comments failed: {e}")
                    try:
                        cur.execute("DELETE FROM work_queue WHERE cluster_id=%s", (cid,))
                    except Exception as e:
                        print(f"cleanup: delete work_queue failed: {e}")
                    try:
                        cur.execute("DELETE FROM tickets WHERE board_id IN "
                                    "(SELECT id FROM boards WHERE cluster_id=%s)", (cid,))
                    except Exception as e:
                        print(f"cleanup: delete tickets failed: {e}")
                    try:
                        cur.execute("DELETE FROM boards WHERE cluster_id=%s", (cid,))
                    except Exception as e:
                        print(f"cleanup: delete boards failed: {e}")
                    try:
                        cur.execute("SELECT id, role_name FROM workers WHERE cluster_id=%s", (cid,))
                        for _, role in cur.fetchall():
                            if role:
                                try:
                                    cur.execute(f'DROP ROLE IF EXISTS "{role}"')
                                except Exception as e:
                                    print(f"cleanup: drop role {role} failed: {e}")
                    except Exception as e:
                        print(f"cleanup: select/drop roles failed: {e}")
                    try:
                        cur.execute("DELETE FROM workers WHERE cluster_id=%s", (cid,))
                    except Exception as e:
                        print(f"cleanup: delete workers failed: {e}")
                    try:
                        cur.execute("DELETE FROM cluster_members WHERE cluster_id=%s", (cid,))
                    except Exception as e:
                        print(f"cleanup: delete cluster_members failed: {e}")
                    try:
                        cur.execute("DELETE FROM clusters WHERE id=%s", (cid,))
                    except Exception as e:
                        print(f"cleanup: delete clusters failed: {e}")
                try:
                    cur.execute("DELETE FROM auth_tokens WHERE user_id IN "
                                "(SELECT id FROM users WHERE email='smoke-v2@example.com')")
                except Exception as e:
                    print(f"cleanup: delete auth_tokens failed: {e}")
                try:
                    cur.execute("DELETE FROM users WHERE email='smoke-v2@example.com'")
                except Exception as e:
                    print(f"cleanup: delete users failed: {e}")
            print("fixtures cleaned")
        except Exception as cleanup_exc:
            print(f"CLEANUP FAILED (fixtures may remain): {cleanup_exc}")


if __name__ == "__main__":
    sys.exit(main())
