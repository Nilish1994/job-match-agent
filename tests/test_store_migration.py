import sqlite3

import pytest

from agent.store import V1, Store
from agent.vault import Vault, VaultError


def single_user_db(path):
    """A database exactly as the single-user version left it (schema v1)."""
    db = sqlite3.connect(path)
    db.executescript(V1)
    db.execute("INSERT INTO jobs VALUES ('j1', 'Dev', 'Acme', 'Melbourne', 'desc', NULL, NULL, 'https://x', NULL, NULL, '[]', 'now')")
    db.execute("""INSERT INTO matches (job_id, score, verdict, reasons, gaps, red_flags, model, scored_at, status, letter,
                  letter_version) VALUES ('j1', 80, 'possible', '[]', '[]', '[]', 'm', 'now', 'approved', 'Dear team', 2)""")
    db.execute("INSERT INTO api_usage VALUES ('now', 'score', 'm', 100, 20, 0)")
    db.execute("INSERT INTO meta VALUES ('feed_cursor', '{\"since\": \"x\"}')")
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()


def test_single_user_data_becomes_the_owners(tmp_path):
    path = str(tmp_path / "agent.db")
    single_user_db(path)
    store = Store(path, owner_chat_id="42")
    owner = store.user_by_chat("42")
    assert owner["id"] == 1 and owner["status"] == "active"
    match = store.for_user(1).match("j1")
    assert match["status"] == "approved" and match["letter"] == "Dear team" and match["letter_version"] == 2
    assert store.usage_since(store.db.execute("SELECT min(at) FROM api_usage").fetchone()[0] and __import__(
        "datetime").datetime(2000, 1, 1, tzinfo=__import__("datetime").timezone.utc), user_id=1)["calls"] == 1
    assert store.get_meta("feed_cursor") == {"since": "x"}
    assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
    Store(path, owner_chat_id="42")  # re-opening is a no-op


def test_existing_data_needs_an_owner(tmp_path):
    path = str(tmp_path / "agent.db")
    single_user_db(path)
    with pytest.raises(RuntimeError, match="OWNER_CHAT_ID"):
        Store(path)


def test_fresh_database(tmp_path):
    store = Store(str(tmp_path / "agent.db"))
    assert store.users() == []


def test_invites_are_single_use(store):
    code = store.create_invite("for Sam")
    assert store.redeem_invite(code, "100", "Sam")["chat_id"] == "100"
    assert store.redeem_invite(code, "200", "Pat") is None
    assert store.redeem_invite("nope", "300", "Lee") is None


def test_delete_user_removes_everything(store):
    user = store.create_user("100", "Sam")
    store.save_jobs([{"id": "j1", "title": "Dev", "skills": []}])
    scoped = store.for_user(user["id"])
    from agent.matcher import JobScore
    scoped.save_score("j1", JobScore(job_id="j1", score=80, verdict="possible", reasons=[], gaps=[], red_flags=[]), "m")
    scoped.record_usage("score", "m", type("U", (), {"input_tokens": 1, "output_tokens": 1})())
    scoped.set_meta("last_manual_run", "today")
    store.await_input("100", "j1", "fullad")
    store.delete_user(user["id"])
    for table, where in [("users", "id = 1"), ("matches", "user_id = 1"), ("api_usage", "user_id = 1"),
                         ("meta", "key LIKE 'user:1:%'"), ("awaiting_input", "chat_id = '100'")]:
        assert store.db.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0] == 0, table
    assert store.job("j1")  # shared job data stays


def test_vault_round_trip_and_wrong_key():
    from cryptography.fernet import Fernet
    vault = Vault(Fernet.generate_key().decode())
    token = vault.encrypt("name: Sam")
    assert b"Sam" not in token and vault.decrypt(token) == "name: Sam"
    with pytest.raises(VaultError):
        Vault(Fernet.generate_key().decode()).decrypt(token)
    with pytest.raises(VaultError):
        Vault("")
