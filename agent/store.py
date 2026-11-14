"""SQLite state: jobs seen, match scores, the approval pipeline and API usage."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT, location TEXT, description TEXT,
    salary_min REAL, salary_max REAL, url TEXT, posted_at TEXT, first_seen_at TEXT, skills TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
-- status: scored -> sent -> approved -> applied -> interview | rejected | no_reply
--                        \\-> skipped
CREATE TABLE IF NOT EXISTS matches (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    verdict TEXT NOT NULL, reasons TEXT NOT NULL, gaps TEXT NOT NULL, red_flags TEXT NOT NULL,
    model TEXT NOT NULL, scored_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scored',
    sent_at TEXT, decided_at TEXT, applied_at TEXT, follow_up_at TEXT, outcome_at TEXT,
    letter TEXT, letter_notes TEXT, letter_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_matches_status ON matches(status, score DESC);
CREATE TABLE IF NOT EXISTS awaiting_input (chat_id TEXT PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS api_usage (
    at TEXT NOT NULL, purpose TEXT NOT NULL, model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0
);
"""

STATUSES = {"scored", "sent", "approved", "skipped", "applied", "interview", "rejected", "no_reply"}


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # meta -----------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                        (key, json.dumps(value)))

    # jobs -----------------------------------------------------------------
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

    def unscored_jobs(self, since: datetime) -> list[dict]:
        rows = self.db.execute(
            """SELECT j.* FROM jobs j LEFT JOIN matches m ON m.job_id = j.id
               WHERE m.job_id IS NULL AND COALESCE(j.posted_at, j.first_seen_at) >= ?
               ORDER BY COALESCE(j.posted_at, j.first_seen_at) DESC""", (iso(since),)).fetchall()
        return [_job(row) for row in rows]

    # matches --------------------------------------------------------------
    def save_score(self, job_id: str, score, model: str) -> None:
        self.db.execute(
            """INSERT INTO matches(job_id, score, verdict, reasons, gaps, red_flags, model, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(job_id) DO NOTHING""",
            (job_id, score.score, score.verdict, json.dumps(score.reasons), json.dumps(score.gaps),
             json.dumps(score.red_flags), model, iso(now())))

    def match(self, job_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM matches WHERE job_id = ?", (job_id,)).fetchone()
        return _match(row) if row else None

    def digest_candidates(self, min_score: int, limit: int) -> list[dict]:
        """Best unsent matches, one per (title, company): job boards often re-list the same role.

        Re-listings of a role already sent (or decided on) are marked skipped so they never resurface.
        """
        rows = self.db.execute(
            """SELECT m.*, j.title, j.company, j.location, j.salary_min, j.salary_max, j.url, j.posted_at
               FROM matches m JOIN jobs j ON j.id = m.job_id
               WHERE m.status = 'scored' AND m.score >= ? AND m.verdict != 'no'
               ORDER BY m.score DESC, COALESCE(j.posted_at, j.first_seen_at) DESC""", (min_score,)).fetchall()
        handled = {(r["t"], r["c"]) for r in self.db.execute(
            """SELECT lower(trim(j.title)) AS t, lower(trim(COALESCE(j.company, ''))) AS c
               FROM matches m JOIN jobs j ON j.id = m.job_id WHERE m.status != 'scored'""")}
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
        self.db.execute(f"UPDATE matches SET {columns} WHERE job_id = ?", (*values.values(), job_id))

    def save_letter(self, job_id: str, letter: str, notes: dict) -> int:
        self.db.execute(
            """UPDATE matches SET letter = ?, letter_notes = ?, letter_version = letter_version + 1
               WHERE job_id = ?""", (letter, json.dumps(notes), job_id))
        return self.db.execute("SELECT letter_version FROM matches WHERE job_id = ?", (job_id,)).fetchone()[0]

    def mark_applied(self, job_id: str, follow_up_days: int) -> None:
        self.set_status(job_id, "applied", follow_up_at=iso(now() + timedelta(days=follow_up_days)))

    def due_follow_ups(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT m.*, j.title, j.company, j.url FROM matches m JOIN jobs j ON j.id = m.job_id
               WHERE m.status = 'applied' AND m.follow_up_at IS NOT NULL AND m.follow_up_at <= ?""",
            (iso(now()),)).fetchall()
        return [_match(row) for row in rows]

    def snooze_follow_up(self, job_id: str, days: int) -> None:
        self.db.execute("UPDATE matches SET follow_up_at = ? WHERE job_id = ?", (iso(now() + timedelta(days=days)), job_id))

    def pipeline(self, statuses: tuple[str, ...]) -> list[dict]:
        marks = ",".join("?" * len(statuses))
        rows = self.db.execute(
            f"""SELECT m.*, j.title, j.company, j.url FROM matches m JOIN jobs j ON j.id = m.job_id
                WHERE m.status IN ({marks}) ORDER BY m.score DESC""", statuses).fetchall()
        return [_match(row) for row in rows]

    def counts(self) -> dict:
        rows = self.db.execute("SELECT status, count(*) AS n FROM matches GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}

    # pending free-text input ---------------------------------------------
    def await_input(self, chat_id: str, job_id: str, kind: str) -> None:
        self.db.execute("INSERT INTO awaiting_input VALUES (?, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                        "job_id = excluded.job_id, kind = excluded.kind", (chat_id, job_id, kind))

    def pop_input(self, chat_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM awaiting_input WHERE chat_id = ?", (chat_id,)).fetchone()
        if row:
            self.db.execute("DELETE FROM awaiting_input WHERE chat_id = ?", (chat_id,))
        return dict(row) if row else None

    # usage ------------------------------------------------------------------
    def record_usage(self, purpose: str, model: str, usage) -> None:
        self.db.execute("INSERT INTO api_usage VALUES (?, ?, ?, ?, ?, ?)", (
            iso(now()), purpose, model, getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0, getattr(usage, "cache_read_input_tokens", 0) or 0))

    def usage_since(self, since: datetime) -> dict:
        row = self.db.execute(
            """SELECT count(*) AS calls, COALESCE(sum(input_tokens), 0) AS input_tokens,
                      COALESCE(sum(output_tokens), 0) AS output_tokens,
                      COALESCE(sum(cache_read_tokens), 0) AS cache_read_tokens
               FROM api_usage WHERE at >= ?""", (iso(since),)).fetchone()
        return dict(row)


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
