"""Settings from environment variables (see .env.example)."""
from dataclasses import dataclass, field
import os
from zoneinfo import ZoneInfo


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _weekday() -> int | None:
    """RUN_WEEKDAY=sunday runs weekly on that day; empty means every day."""
    value = os.environ.get("RUN_WEEKDAY", "").strip().lower()
    if not value:
        return None
    if value not in WEEKDAYS:
        raise ValueError(f"RUN_WEEKDAY must be one of {', '.join(WEEKDAYS)}")
    return WEEKDAYS.index(value)


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


@dataclass(frozen=True)
class Settings:
    telegram_token: str = field(default_factory=lambda: os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
    # The owner's chat: admin commands, invite approvals, budget alerts. Empty = setup mode
    # (the bot replies with your chat id). TELEGRAM_CHAT_ID is the pre-multi-user name for it.
    telegram_chat_id: str = field(default_factory=lambda: (os.environ.get("OWNER_CHAT_ID")
                                                           or os.environ.get("TELEGRAM_CHAT_ID", "")).strip())
    data_key: str = field(default_factory=lambda: os.environ.get("DATA_KEY", "").strip())
    feed_url: str = field(default_factory=lambda: os.environ.get("JOBS_FEED_URL", "http://api:8000/feed/jobs"))
    profile_path: str = field(default_factory=lambda: os.environ.get("PROFILE_PATH", "/data/profile.yaml"))
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", "/data/agent.db"))
    score_model: str = field(default_factory=lambda: os.environ.get("SCORE_MODEL", "claude-opus-5-5"))
    draft_model: str = field(default_factory=lambda: os.environ.get("DRAFT_MODEL", "claude-opus-5-5"))
    timezone: ZoneInfo = field(default_factory=lambda: ZoneInfo(os.environ.get("TIMEZONE", "Australia/Melbourne")))
    daily_hour: int = field(default_factory=lambda: _int("DAILY_HOUR", 8))
    run_weekday: int | None = field(default_factory=_weekday)  # 0 = Monday ... 6 = Sunday
    min_score: int = field(default_factory=lambda: _int("MIN_SCORE", 70))
    max_digest: int = field(default_factory=lambda: _int("MAX_DIGEST", 5))
    score_batch: int = field(default_factory=lambda: _int("SCORE_BATCH", 8))
    max_scored_per_run: int = field(default_factory=lambda: _int("MAX_SCORED_PER_RUN", 150))  # per user
    max_drafts_per_day: int = field(default_factory=lambda: _int("MAX_DRAFTS_PER_DAY", 10))  # per user
    weekly_budget_usd: float = field(default_factory=lambda: float(os.environ.get("WEEKLY_BUDGET_USD", "") or 15))
    max_job_age_days: int = field(default_factory=lambda: _int("MAX_JOB_AGE_DAYS", 14))
    follow_up_days: int = field(default_factory=lambda: _int("FOLLOW_UP_DAYS", 7))
