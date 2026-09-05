"""app.kaggle_accounts: CRUD, claim nguyên tử, cooldown tự hồi phục, quota 7 ngày."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from app import db, kaggle_accounts as ka


def _conn(tmp_path=None):
    conn = db.connect(str(tmp_path / "app.db") if tmp_path else ":memory:")
    db.init_schema(conn)
    return conn


def _iso(delta_seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def test_create_and_list_account():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    accounts = ka.list_accounts(conn)
    assert len(accounts) == 1
    assert accounts[0]["id"] == account_id
    assert accounts[0]["status"] == "idle"


def test_get_account_returns_none_for_unknown_id():
    conn = _conn()
    assert ka.get_account(conn, 999) is None


def test_update_account_changes_label_and_username_but_keeps_key_when_blank():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.update_account(conn, account_id, label="acc1-renamed", username="user1", api_key="")
    row = ka.get_account(conn, account_id)
    assert row["label"] == "acc1-renamed"
    assert row["api_key"] == "key1"


def test_update_account_replaces_key_when_given():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.update_account(conn, account_id, label="acc1", username="user1", api_key="key2")
    assert ka.get_account(conn, account_id)["api_key"] == "key2"


def test_set_disabled_flips_status_and_re_enable_clears_cooldown():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.set_disabled(conn, account_id, True)
    assert ka.get_account(conn, account_id)["status"] == "disabled"
    ka.set_disabled(conn, account_id, False)
    row = ka.get_account(conn, account_id)
    assert row["status"] == "idle"
    assert row["cooldown_until"] is None


def test_claim_moves_account_to_busy_and_stamps_job_id():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    claimed = ka.claim_idle_account(conn, job_id=42)
    assert claimed["id"] == account_id
    assert claimed["status"] == "busy"
    assert ka.get_account(conn, account_id)["in_use_by_job_id"] == 42


def test_claim_skips_busy_and_disabled_accounts():
    conn = _conn()
    ka.create_account(conn, "acc1", "user1", "key1")
    ka.claim_idle_account(conn, job_id=1)  # now busy
    second = ka.create_account(conn, "acc2", "user2", "key2")
    ka.set_disabled(conn, second, True)
    assert ka.claim_idle_account(conn, job_id=2) is None


def test_claim_self_heals_an_expired_cooldown():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.release_account(conn, account_id, cooldown_until=_iso(-10))
    claimed = ka.claim_idle_account(conn, job_id=5)
    assert claimed["id"] == account_id


def test_claim_respects_a_future_cooldown():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.release_account(conn, account_id, cooldown_until=_iso(3600))
    assert ka.claim_idle_account(conn, job_id=5) is None


def test_claim_prefers_the_account_touched_longest_ago():
    conn = _conn()
    older = ka.create_account(conn, "acc1", "user1", "key1")
    newer = ka.create_account(conn, "acc2", "user2", "key2")
    conn.execute("UPDATE kaggle_account SET updated_at=? WHERE id=?", (_iso(-3600), older))
    conn.execute("UPDATE kaggle_account SET updated_at=? WHERE id=?", (_iso(-10), newer))
    conn.commit()
    claimed = ka.claim_idle_account(conn, job_id=1)
    assert claimed["id"] == older


def test_claim_is_atomic_across_threads(tmp_path):
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    conn.close()

    claimed: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(10)

    def worker(n):
        c = db.connect(str(tmp_path / "app.db"))
        start.wait()
        account = ka.claim_idle_account(c, job_id=n)
        if account is not None:
            with lock:
                claimed.append(account["id"])
        c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 1


def test_release_without_cooldown_returns_to_idle():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.claim_idle_account(conn, job_id=1)
    ka.release_account(conn, account_id)
    row = ka.get_account(conn, account_id)
    assert row["status"] == "idle"
    assert row["in_use_by_job_id"] is None


def test_release_with_cooldown_sets_status_and_timestamp():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.claim_idle_account(conn, job_id=1)
    until = _iso(3600)
    ka.release_account(conn, account_id, cooldown_until=until)
    row = ka.get_account(conn, account_id)
    assert row["status"] == "cooldown"
    assert row["cooldown_until"] == until
    assert row["in_use_by_job_id"] is None


def test_delete_refuses_an_account_in_use():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.claim_idle_account(conn, job_id=1)
    assert ka.delete_account(conn, account_id) is False
    ka.release_account(conn, account_id)
    assert ka.delete_account(conn, account_id) is True
    assert ka.get_account(conn, account_id) is None


def test_record_usage_start_and_finish(conn=None):
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    usage_id = ka.record_usage_start(conn, account_id, "user1/slug")
    row = conn.execute("SELECT * FROM kaggle_usage WHERE id=?", (usage_id,)).fetchone()
    assert row["account_id"] == account_id
    assert row["kernel_ref"] == "user1/slug"
    assert row["gpu_seconds"] is None
    ka.record_usage_finish(conn, usage_id, gpu_seconds=120)
    row = conn.execute("SELECT * FROM kaggle_usage WHERE id=?", (usage_id,)).fetchone()
    assert row["gpu_seconds"] == 120
    assert row["finished_at"] is not None


def test_remaining_quota_subtracts_last_7_days(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "kaggle_weekly_gpu_quota_hours", 10)
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    usage_id = ka.record_usage_start(conn, account_id, "user1/slug")
    ka.record_usage_finish(conn, usage_id, gpu_seconds=3600 * 4)
    assert ka.remaining_quota_seconds(conn, account_id) == 3600 * 6


def test_remaining_quota_ignores_usage_older_than_7_days(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "kaggle_weekly_gpu_quota_hours", 10)
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    old = _iso(-8 * 24 * 3600)
    now = _iso()
    conn.execute(
        "INSERT INTO kaggle_usage (account_id, kernel_ref, started_at, finished_at, gpu_seconds, created_at) "
        "VALUES (?, 'user1/slug', ?, ?, ?, ?)",
        (account_id, old, old, 3600 * 9, now),
    )
    conn.commit()
    assert ka.remaining_quota_seconds(conn, account_id) == 3600 * 10


def test_remaining_quota_never_negative(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "kaggle_weekly_gpu_quota_hours", 1)
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    usage_id = ka.record_usage_start(conn, account_id, "user1/slug")
    ka.record_usage_finish(conn, usage_id, gpu_seconds=3600 * 5)
    assert ka.remaining_quota_seconds(conn, account_id) == 0


def test_remaining_quota_ignores_other_accounts():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    other_id = ka.create_account(conn, "acc2", "user2", "key2")
    usage_id = ka.record_usage_start(conn, other_id, "user2/slug")
    ka.record_usage_finish(conn, usage_id, gpu_seconds=3600 * 20)
    assert ka.remaining_quota_seconds(conn, account_id) > 0


def test_earliest_quota_reset_is_none_with_no_usage():
    conn = _conn()
    ka.create_account(conn, "acc1", "user1", "key1")
    assert ka.earliest_quota_reset(conn) is None


def test_earliest_quota_reset_is_7_days_after_the_oldest_counted_usage():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    started = _iso(-3600)
    conn.execute(
        "INSERT INTO kaggle_usage (account_id, kernel_ref, started_at, finished_at, gpu_seconds, created_at) "
        "VALUES (?, 'user1/slug', ?, ?, 60, ?)",
        (account_id, started, _iso(), _iso()),
    )
    conn.commit()
    reset = ka.earliest_quota_reset(conn)
    expected = (datetime.fromisoformat(started) + timedelta(days=7)).isoformat()
    assert reset == expected
