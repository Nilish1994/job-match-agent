"""Entry point.

    python -m agent serve   # long-poll Telegram; weekly matching for every active user; daily follow-ups
    python -m agent run     # one matching run for every active user, now
    python -m agent check   # validate configuration without calling Claude or Telegram
"""
import logging
import os
import sys
import time

import anthropic
import httpx

from agent.bot import Bot
from agent.config import Settings
from agent.profile import Profile
from agent.store import Store
from agent.telegram import Telegram, TelegramError
from agent.vault import Vault

log = logging.getLogger("agent")


def build(settings: Settings, http: httpx.Client, *, connect: bool = True) -> Bot:
    store = Store(settings.db_path, owner_chat_id=settings.telegram_chat_id or None)
    telegram = Telegram(settings.telegram_token, http)
    username = telegram.get_me().get("username", "") if connect else ""
    bot = Bot(settings, store, telegram, anthropic.Anthropic(), http, Vault(settings.data_key), bot_username=username)
    bot.ensure_owner(settings.profile_path if os.path.exists(settings.profile_path) else None)
    return bot


def serve(bot: Bot) -> None:
    offset = bot.store.get_meta("telegram_offset")
    log.info("serving as @%s; owner configured: %s; users: %d", bot.bot_username, bool(bot.owner),
             len(bot.store.users()))
    while True:
        try:
            if bot.owner and bot.weekly_due():
                bot.run_weekly()
            elif bot.owner and bot.follow_ups_due():
                bot.check_follow_ups()
            for update in bot.tg.updates(offset, wait=25):
                offset = update["update_id"] + 1
                bot.store.set_meta("telegram_offset", offset)
                try:
                    bot.handle(update)
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
        Vault(settings.data_key)  # raises if missing or malformed
        if os.path.exists(settings.profile_path):
            profile = Profile.load(settings.profile_path)
            print(f"owner profile ok: {profile.name}, {len(profile.skills)} skills")
        print(f"telegram token set: {bool(settings.telegram_token)}; owner chat set: {bool(settings.telegram_chat_id)}; "
              f"data key ok; weekly budget ${settings.weekly_budget_usd:.2f}")
        return 0
    with httpx.Client(follow_redirects=False) as http:
        bot = build(settings, http)
        if command == "run":
            print(bot.run_weekly())
            return 0
        if command == "serve":
            serve(bot)
            return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
