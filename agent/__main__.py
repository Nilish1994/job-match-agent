"""Entry point.

    python -m agent serve   # long-poll Telegram and run the daily match at DAILY_HOUR
    python -m agent run     # one matching run now (sends the digest)
    python -m agent check   # validate config and profile without calling any API
"""
import logging
import sys
import time

import anthropic
import httpx

from agent.app import Agent
from agent.config import Settings
from agent.matcher import Matcher
from agent.profile import Profile
from agent.store import Store
from agent.telegram import Telegram, TelegramError

log = logging.getLogger("agent")


def build(settings: Settings, http: httpx.Client) -> Agent:
    store = Store(settings.db_path)
    profile = Profile.load(settings.profile_path)
    matcher = Matcher(anthropic.Anthropic(), profile, settings.score_model, settings.draft_model,
                      usage_cb=store.record_usage)
    return Agent(settings, store, Telegram(settings.telegram_token, http), matcher, http)


def serve(agent: Agent) -> None:
    offset = agent.store.get_meta("telegram_offset")
    log.info("serving; chat configured: %s", bool(agent.chat))
    while True:
        try:
            if agent.chat and agent.daily_due():
                agent.run_daily()
            for update in agent.tg.updates(offset, wait=25):
                offset = update["update_id"] + 1
                agent.store.set_meta("telegram_offset", offset)
                try:
                    agent.handle(update)
                except Exception:  # one bad update must not stop the bot
                    log.exception("update %s failed", update.get("update_id"))
        except (httpx.HTTPError, TelegramError) as exc:
            log.warning("network problem (%s); retrying in 10s", type(exc).__name__)
            time.sleep(10)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL at INFO, and Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    command = argv[0] if argv else "serve"
    settings = Settings()
    if command == "check":
        profile = Profile.load(settings.profile_path)
        print(f"profile ok: {profile.name}, {len(profile.skills)} skills, {len(profile.projects)} projects")
        print(f"telegram token set: {bool(settings.telegram_token)}; chat id set: {bool(settings.telegram_chat_id)}")
        return 0
    with httpx.Client(follow_redirects=False) as http:
        agent = build(settings, http)
        if command == "run":
            if not agent.chat:
                print("Set TELEGRAM_CHAT_ID first (message the bot to get it).", file=sys.stderr)
                return 1
            print(agent.run_daily())
            return 0
        if command == "serve":
            serve(agent)
            return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
