"""Settings from environment variables (see .env.example)."""
from dataclasses import dataclass, field
import os
from zoneinfo import ZoneInfo


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    # Only this chat may control the bot. Empty = setup mode (the bot replies with your chat id).
    telegram_chat_id: str = field(default_factory=lambda: os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    feed_url: str = field(default_factory=lambda: os.environ.get("JOBS_FEED_URL", "http://api:8000/feed/jobs"))
    profile_path: str = field(default_factory=lambda: os.environ.get("PROFILE_PATH", "/data/profile.yaml"))
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "/data/agent.db"))
    score_model: str = field(default_factory=lambda: os.environ.get("SCORE_MODEL", "claude-opus-5-5"))
    draft_model: str = field(default_factory=lambda: os.environ.get("DRAFT_MODEL", "claude-opus-5-5"))
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo(os.environ.get("TIMEZONE", "Australia/Melbourne")))
    daily_hour: int = field(default_factory=lambda: _int("DAILY_HOUR", 8))
    min_score: int = field(default_factory=lambda: _int("MIN_SCORE", 70))
    max_digest: int = field(default_factory=lambda: _int("MAX_DIGEST", 5))
    score_batch: int = field(default_factory=lambda: _int("SCORE_BATCH", 8))
    max_scored_per_run: int = field(default_factory=lambda: _int("MAX_SCORED_PER_RUN", 120))
    max_job_age_days: int = field(default_factory=lambda: _int("MAX_JOB_AGE_DAYS", 14))
    follow_up_days: int = field(default_factory=lambda: _int("FOLLOW_UP_DAYS", 7))
