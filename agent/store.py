"""SQLite state for many users: shared jobs, per-user matches, invites and API usage.

`Store` holds everything shared (jobs, users, invites, meta). `store.for_user(id)`
returns a `UserStore` whose every query is scoped to that user, so per-user code
can't read or change another user's matches by construction.

Schema versions are tracked with PRAGMA user_version and migrated in place.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3

V1 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT, location TEXT, description TEXT,
    salary_min REAL, salary_max REAL, url TEXT, posted_at TEXT, first_seen_at TEXT, skills TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS matches (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    verdict TEXT NOT NULL, reasons TEXT NOT NULL, gaps TEXT NOT NULL, red_flags TEXT NOT NULL,
    model TEXT NOT NULL, scored_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scored',
    sent_at TEXT, decided_at TEXT, applied_at TEXT, follow_up_at TEXT, outcome_at TEXT,
    letter TEXT, letter_notes TEXT, letter_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS awaiting_input (chat_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_usage (
    at TEXT NOT NULL, purpose TEXT NOT NULL, model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0
);
"""

V2 = """
-- status: consent -> cv -> prefs -> review -> active <-> paused
CREATE TABLE users (
    id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL UNIQUE, name TEXT,
    status TEXT NOT NULL DEFAULT 'consent', profile_enc BLOB, onboarding TEXT NOT NULL DEFAULT '{}',
    invite_code TEXT, consent_at TEXT, created_at TEXT NOT NULL
);
CREATE TABLE invites (
    code TEXT PRIMARY KEY, note TEXT, created_at TEXT NOT NULL,
    used_by INTEGER REFERENCES users(id) ON DELETE SET NULL, used_at TEXT
);
CREATE TABLE invite_requests (
    chat_id TEXT PRIMARY KEY, name TEXT, requested_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
);
-- matches become per user: status scored -> sent -> approved -> applied -> interview | rejected | no_reply, or skipped
CREATE TABLE matches_v2 (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    verdict TEXT NOT NULL, reasons TEXT NOT NULL, gaps TEXT NOT NULL, red_flags TEXT NOT NULL,
    model TEXT NOT NULL, scored_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scored',
    sent_at TEXT, decided_at TEXT, applied_at TEXT, follow_up_at TEXT, outcome_at TEXT,
    letter TEXT, letter_notes TEXT, letter_version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, job_id)
);
CREATE INDEX ix_matches_user_status ON matches_v2(user_id, status, score DESC);
ALTER TABLE api_usage ADD COLUMN user_id INTEGER;
"""

STATUSES = {"scored", "sent", "approved", "skipped", "applied", "interview", "rejected", "no_reply"}
USER_STATUSES = {"consent", "cv", "prefs", "review", "active", "paused"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str, owner_chat_id: str | None = None):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self._migrate(owner_chat_id)

    def _migrate(self, owner_chat_id: str | None) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            self.db.executescript(V1)
            self.db.execute("PRAGMA user_version = 1")
            version = 1
        if version < 2:
            with self.transaction() as db:
                for statement in filter(str.strip, V2.split(";")):
                    db.execute(statement)
                legacy = db.execute("SELECT count(*) FROM matches").fetchone()[0]
                if legacy:
                    # A single-user install becomes user #1, the owner. Its chat id must be known.
                    if not owner_chat_id:
                        raise RuntimeError("Existing data found: set OWNER_CHAT_ID so it can be assigned to its owner")
                    db.execute("INSERT INTO users (id, chat_id, status, created_at, consent_at) VALUES (1, ?, 'active', ?, ?)",
                               (str(owner_chat_id), iso(now()), iso(now())))
                    db.execute("INSERT INTO matches_v2 SELECT 1, * FROM matches")
                    db.execute("UPDATE api_usage SET user_id = 1")
                db.execute("DROP TABLE matches")
                db.execute("ALTER TABLE matches_v2 RENAME TO matches")
                db.execute("PRAGMA user_version = 2")

    @contextmanager
    def transaction(self):
        if self.db.in_transaction:  # nested use joins the outer transaction
            yield self.db
            return
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def for_user(self, user_id: int) -> "UserStore":
        return UserStore(self, user_id)

    # meta -----------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value)))

    # jobs (shared) ----------------------------------------------------------
    def save_jobs(self, items: list[dict]) -> int:
        with self.transaction() as db:
            for job in items:
                db.execute(
                    """INSERT INTO jobs(id, title, company, location, description, salary_min, salary_max, url,
                                        posted_at, first_seen_at, skills, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET title = excluded.title, description = excluded.description,
                         salary_min = excluded.salary_min, salary_max = excluded.salary_max, skills = excluded.skills""",
                    (str(job["id"]), job["title"], job.get("company_name"), job.get("location"), job.get("description"),
                     _num(job.get("salary_min")), _num(job.get("salary_max")), job.get("source_url"),
                     job.get("posted_at"), job.get("first_seen_at"), json.dumps(job.get("skills", [])), iso(now())))
        return len(items)

    def job(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _job(row) if row else None

    # users -------------------------------------------------------------------
    def user_by_chat(self, chat_id) -> dict | None:
        row = self.db.execute("SELECT * FROM users WHERE chat_id = ?", (str(chat_id),)).fetchone()
        return _user(row) if row else None

    def user(self, user_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user(row) if row else None

    def users(self, statuses: tuple[str, ...] | None = None) -> list[dict]:
        if statuses:
            marks = ",".join("?" * len(statuses))
            rows = self.db.execute(f"SELECT * FROM users WHERE status IN ({marks}) ORDER BY id", statuses).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM users ORDER BY id").fetchall()
        return [_user(row) for row in rows]

    def create_user(self, chat_id, name: str | None, invite_code: str | None = None) -> dict:
        self.db.execute("INSERT INTO users (chat_id, name, invite_code, created_at) VALUES (?, ?, ?, ?)",
                        (str(chat_id), name, invite_code, iso(now())))
        return self.user_by_chat(chat_id)

    def update_user(self, user_id: int, **fields) -> None:
        if "status" in fields and fields["status"] not in USER_STATUSES:
            raise ValueError(fields["status"])
        if "onboarding" in fields:
            fields["onboarding"] = json.dumps(fields["onboarding"])
        columns = ", ".join(f"{key} = ?" for key in fields)
        self.db.execute(f"UPDATE users SET {columns} WHERE id = ?", (*fields.values(), user_id))

    def delete_user(self, user_id: int) -> None:
        """Remove a user and everything that belongs to them."""
        with self.transaction() as db:
            chat = db.execute("SELECT chat_id FROM users WHERE id = ?", (user_id,)).fetchone()
            db.execute("DELETE FROM matches WHERE user_id = ?", (user_id,))
            db.execute("DELETE FROM api_usage WHERE user_id = ?", (user_id,))
            db.execute("DELETE FROM meta WHERE key LIKE ?", (f"user:{user_id}:%",))
            if chat:
                db.execute("DELETE FROM awaiting_input WHERE chat_id = ?", (chat["chat_id"],))
                db.execute("DELETE FROM invite_requests WHERE chat_id = ?", (chat["chat_id"],))
            db.execute("UPDATE invites SET used_by = NULL WHERE used_by = ?", (user_id,))
            db.execute("DELETE FROM users WHERE id = ?", (user_id,))

    # invites -------------------------------------------------------------------
    def create_invite(self, note: str | None = None) -> str:
        code = secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")
        self.db.execute("INSERT INTO invites (code, note, created_at) VALUES (?, ?, ?)", (code, note, iso(now())))
        return code

    def redeem_invite(self, code: str, chat_id, name: str | None) -> dict | None:
        """Create the user if the code exists and is unused. Single use, atomically."""
        with self.transaction() as db:
            row = db.execute("SELECT * FROM invites WHERE code = ? AND used_at IS NULL", (code,)).fetchone()
            if not row or self.user_by_chat(chat_id):
                return None
            user = self.create_user(chat_id, name, invite_code=code)
            db.execute("UPDATE invites SET used_by = ?, used_at = ? WHERE code = ?", (user["id"], iso(now()), code))
        return user

    def request_invite(self, chat_id, name: str | None) -> bool:
        """Record a request; False if this chat already has one pending."""
        existing = self.db.execute("SELECT status FROM invite_requests WHERE chat_id = ?", (str(chat_id),)).fetchone()
        if existing and existing["status"] == "pending":
            return False
        self.db.execute("""INSERT INTO invite_requests (chat_id, name, requested_at) VALUES (?, ?, ?)
                           ON CONFLICT(chat_id) DO UPDATE SET status = 'pending', requested_at = excluded.requested_at,
                           name = excluded.name""", (str(chat_id), name, iso(now())))
        return True

    def close_request(self, chat_id, status: str) -> dict | None:
        row = self.db.execute("SELECT * FROM invite_requests WHERE chat_id = ? AND status = 'pending'",
                              (str(chat_id),)).fetchone()
        if row:
            self.db.execute("UPDATE invite_requests SET status = ? WHERE chat_id = ?", (status, str(chat_id)))
        return dict(row) if row else None

    def pending_requests(self) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM invite_requests WHERE status = 'pending' ORDER BY requested_at").fetchall()]

    # free-text input -----------------------------------------------------------
    def await_input(self, chat_id: str, job_id: str, kind: str) -> None:
        self.db.execute("INSERT INTO awaiting_input VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                        "job_id = excluded.job_id, kind = excluded.kind", (str(chat_id), job_id, kind))

    def pop_input(self, chat_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM awaiting_input WHERE chat_id = ?", (str(chat_id),)).fetchone()
        if row:
            self.db.execute("DELETE FROM awaiting_input WHERE chat_id = ?", (str(chat_id),))
        return dict(row) if row else None

    # usage -------------------------------------------------------------------
    def usage_since(self, since: datetime, user_id: int | None = None) -> dict:
        where, params = "at >= ?", [iso(since)]
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(user_id)
        row = self.db.execute(
            f"""SELECT count(*) AS calls, COALESCE(sum(input_tokens), 0) AS input_tokens,
                       COALESCE(sum(output_tokens), 0) AS output_tokens,
                       COALESCE(sum(cache_read_tokens), 0) AS cache_read_tokens
                FROM api_usage WHERE {where}""", params).fetchone()
        return dict(row)

    def usage_by_model_since(self, since: datetime) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            """SELECT model, COALESCE(sum(input_tokens), 0) AS input_tokens, COALESCE(sum(output_tokens), 0) AS output_tokens,
                      COALESCE(sum(cache_read_tokens), 0) AS cache_read_tokens
               FROM api_usage WHERE at >= ? GROUP BY model""", (iso(since),)).fetchall()]


class UserStore:
    """Every query here is scoped to one user. Shared tables are reached through `root`."""

    def __init__(self, root: Store, user_id: int):
        self.root, self.user_id, self.db = root, user_id, root.db

    def transaction(self):
        return self.root.transaction()

    def job(self, job_id: str) -> dict | None:
        return self.root.job(job_id)

    def save_jobs(self, items: list[dict]) -> int:
        return self.root.save_jobs(items)

    # per-user meta (scheduling stamps, rate limits) -----------------------------------
    def get_meta(self, key: str, default=None):
        return self.root.get_meta(f"user:{self.user_id}:{key}", default)

    def set_meta(self, key: str, value) -> None:
        self.root.set_meta(f"user:{self.user_id}:{key}", value)

    def await_input(self, chat_id: str, job_id: str, kind: str) -> None:
        self.root.await_input(chat_id, job_id, kind)

    def pop_input(self, chat_id: str) -> dict | None:
        return self.root.pop_input(chat_id)

    # matches -------------------------------------------------------------------
    def unscored_jobs(self, since: datetime) -> list[dict]:
        rows = self.db.execute(
            """SELECT j.* FROM jobs j LEFT JOIN matches m ON m.job_id = j.id AND m.user_id = ?
               WHERE m.job_id IS NULL AND COALESCE(j.posted_at, j.first_seen_at) >= ?
               ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC""", (self.user_id, iso(since))).fetchall()
        return [_job(row) for row in rows]

    def save_score(self, job_id: str, score, model: str) -> None:
        self.db.execute(
            """INSERT INTO matches(user_id, job_id, score, verdict, reasons, gaps, red_flags, model, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, job_id) DO NOTHING""",
            (self.user_id, job_id, score.score, score.verdict, json.dumps(score.reasons), json.dumps(score.gaps),
             json.dumps(score.red_flags), model, iso(now())))

    def match(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM matches WHERE user_id = ? AND job_id = ?", (self.user_id, job_id)).fetchone()
        return _match(row) if row else None

    def digest_candidates(self, min_score: int, limit: int) -> list[dict]:
        """Best unsent matches, one per (title, company): job boards often re-list the same role.

        Re-listings of a role already sent (or decided on) are marked skipped so they never resurface.
        """
        rows = self.db.execute(
            """SELECT m.*, j.title, j.company, j.location, j.salary_min, j.salary_max, j.url, j.posted_at
               FROM matches m JOIN jobs j ON j.id = m.job_id
               WHERE m.user_id = ? AND m.status = 'scored' AND m.score >= ? AND m.verdict != 'no'
               ORDER BY m.score DESC, COALESCE(j.posted_at, j.first_seen_at) DESC""",
            (self.user_id, min_score)).fetchall()
        handled = {(r["t"], r["c"]) for r in self.db.execute(
            """SELECT lower(trim(j.title)) AS t, lower(trim(COALESCE(j.company, ''))) AS c
               FROM matches m JOIN jobs j ON j.id = m.job_id WHERE m.user_id = ? AND m.status != 'scored'""",
            (self.user_id,))}
        picked = []
        for row in rows:
            key = (row["title"].strip().lower(), (row["company"] or "").strip().lower())
            if key in handled:
                self.set_status(row["job_id"], "skipped")
                continue
            handled.add(key)
            if len(picked) < limit:
                picked.append(_match(row))
        return picked

    def set_status(self, job_id: str, status: str, **fields) -> None:
        if status not in STATUSES:
            raise ValueError(status)
        stamp = {"sent": "sent_at", "approved": "decided_at", "skipped": "decided_at", "applied": "applied_at",
                 "interview": "outcome_at", "rejected": "outcome_at", "no_reply": "outcome_at"}.get(status)
        values = {"status": status, **fields}
        if stamp:
            values[stamp] = iso(now())
        columns = ", ".join(f"{key} = ?" for key in values)
        self.db.execute(f"UPDATE matches SET {columns} WHERE user_id = ? AND job_id = ?",
                        (*values.values(), self.user_id, job_id))

    def save_letter(self, job_id: str, letter: str, notes: dict) -> int:
        self.db.execute(
            """UPDATE matches SET letter = ?, letter_notes = ?, letter_version = letter_version + 1
               WHERE user_id = ? AND job_id = ?""", (letter, json.dumps(notes), self.user_id, job_id))
        return self.db.execute("SELECT letter_version FROM matches WHERE user_id = ? AND job_id = ?",
                               (self.user_id, job_id)).fetchone()[0]

    def mark_applied(self, job_id: str, follow_up_days: int) -> None:
        self.set_status(job_id, "applied", follow_up_at=iso(now() + timedelta(days=follow_up_days)))

    def due_follow_ups(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT m.*, j.title, j.company, j.url FROM matches m JOIN jobs j ON j.id = m.job_id
               WHERE m.user_id = ? AND m.status = 'applied' AND m.follow_up_at IS NOT NULL AND m.follow_up_at <= ?""",
            (self.user_id, iso(now()))).fetchall()
        return [_match(row) for row in rows]

    def snooze_follow_up(self, job_id: str, days: int) -> None:
        self.db.execute("UPDATE matches SET follow_up_at = ? WHERE user_id = ? AND job_id = ?",
                        (iso(now() + timedelta(days=days)), self.user_id, job_id))

    def pipeline(self, statuses: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        rows = self.db.execute(
            f"""SELECT m.*, j.title, j.company, j.url FROM matches m JOIN jobs j ON j.id = m.job_id
                WHERE m.user_id = ? AND m.status IN ({marks}) ORDER BY m.score DESC""",
            (self.user_id, *statuses)).fetchall()
        return [_match(row) for row in rows]

    def counts(self) -> dict:
        rows = self.db.execute("SELECT status, count(*) AS n FROM matches WHERE user_id = ? GROUP BY status",
                               (self.user_id,)).fetchall()
        return {row["status"]: row["n"] for row in rows}

    # usage -------------------------------------------------------------------
    def record_usage(self, purpose: str, model: str, usage) -> None:
        self.db.execute(
            "INSERT INTO api_usage (at, purpose, model, input_tokens, output_tokens, cache_read_tokens, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (iso(now()), purpose, model, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0,
             getattr(usage, "cache_read_input_tokens", 0) or 0, self.user_id))

    def usage_since(self, since: datetime) -> dict:
        return self.root.usage_since(since, user_id=self.user_id)

    def drafts_since(self, since: datetime) -> int:
        return self.db.execute("SELECT count(*) FROM api_usage WHERE user_id = ? AND purpose = 'letter' AND at >= ?",
                               (self.user_id, iso(since))).fetchone()[0]


def _num(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _job(row) -> dict:
    job = dict(row)
    job["skills"] = json.loads(job["skills"])
    return job


def _match(row) -> dict:
    match = dict(row)
    for key in ("reasons", "gaps", "red_flags"):
        if key in match and match[key] is not None:
            match[key] = json.loads(match[key])
    if match.get("letter_notes"):
        match["letter_notes"] = json.loads(match["letter_notes"])
    return match


def _user(row) -> dict:
    user = dict(row)
    user["onboarding"] = json.loads(user.get("onboarding") or "{}")
    return user
