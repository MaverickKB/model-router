"""Saved session tables drop expired rows while the process is running."""

import time

from gateway.store import Store


def test_saving_a_session_deletes_expired_operator_rows(tmp_path):
    store = Store(str(tmp_path / "state"))
    now = time.time()
    store.save_operator_session("operator-expired", now - 10)
    store.save_operator_session("operator-live", now + 600)

    rows = store.db.execute("SELECT digest FROM operator_sessions ORDER BY digest").fetchall()
    assert [row[0] for row in rows] == ["operator-live"]


def test_saving_a_session_deletes_expired_portal_rows(tmp_path):
    store = Store(str(tmp_path / "state"))
    now = time.time()
    store.save_portal_session("portal-expired", "account-1", now - 10)
    store.save_portal_session("portal-live", "account-1", now + 600)

    rows = store.db.execute("SELECT digest FROM portal_sessions ORDER BY digest").fetchall()
    assert [row[0] for row in rows] == ["portal-live"]
