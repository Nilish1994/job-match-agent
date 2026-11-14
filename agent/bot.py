"""JobPilot's Telegram front door: routes every update to the right user, runs onboarding,
invites and owner admin, and schedules the weekly matching run across all active users.

Per-user matching, approvals and application kits live in agent.app.Agent; every Agent is
built with a UserStore scoped to one user, so one person's buttons can never touch another's data.
"""
from datetime import datetime, timedelta
import logging

import anthropic
import httpx

from agent import feed, onboarding
from agent.app import HELP, PRICES, Agent
from agent.config import Settings
from agent.matcher import Matcher, Refused, extract_profile
from agent.profile import Profile
from agent.store import Store, now
from agent.telegram import Telegram, TelegramError, button, esc
from agent.vault import Vault

log = logging.getLogger("agent")

PRIVACY = (
    "🔒 <b>Before we start</b>\n"
    "• I keep a profile built from your CV (encrypted), your job matches and application statuses.\n"
    "• I <b>don't</b> keep your CV file: I read it once and discard it.\n"
    "• Your profile text is sent to Anthropic's Claude API to score jobs and draft applications.\n"
    "• I never apply to jobs for you, and never invent experience.\n"
    "• /deleteme removes everything, any time.\n\n"
    "JobPilot is a free beta. Matching covers IT jobs in Melbourne from Adzuna."
)
INVITE_ONLY = ("👋 <b>JobPilot</b> is an AI job-search copilot for Australian tech roles, "
               "currently in an invite-only beta.\n\n"
               "Invites are personal: you'll need an invite link to start. "
               "To ask for one, see jobpilot.imisstech.com.au.")
REQUESTS_CLOSED = "Invites are personal now. See jobpilot.imisstech.com.au to ask for one."
FIRST_RUN_LIMIT = 60


def week_start(at: datetime, tz) -> datetime:
    local = at.astimezone(tz)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


class Bot:
    def __init__(self, settings: Settings, store: Store, telegram: Telegram, client, http: httpx.Client | None,
                 vault: Vault, bot_username: str = ""):
        self.settings, self.store, self.tg, self.client, self.http = settings, store, telegram, client, http
        self.vault, self.bot_username = vault, bot_username

    @property
    def owner(self) -> str:
        return self.settings.telegram_chat_id

    # ------------------------------------------------------------------ users and agents
    def profile(self, user: dict) -> Profile | None:
        return Profile.from_yaml(self.vault.decrypt(user["profile_enc"])) if user.get("profile_enc") else None

    def save_profile(self, user: dict, profile: Profile) -> None:
        self.store.update_user(user["id"], profile_enc=self.vault.encrypt(profile.to_yaml()), name=profile.name)

    def agent_for(self, user: dict) -> Agent:
        scoped = self.store.for_user(user["id"])
        matcher = Matcher(self.client, self.profile(user), self.settings.score_model, self.settings.draft_model,
                          usage_cb=scoped.record_usage)
        return Agent(self.settings, scoped, self.tg, matcher, self.http, chat_id=user["chat_id"],
                     budget_ok=self.budget_ok)

    def ensure_owner(self, profile_path: str | None) -> None:
        """Make the owner's chat a user, importing a profile.yaml from the single-user setup if present."""
        if not self.owner:
            return
        user = self.store.user_by_chat(self.owner)
        try:
            imported = Profile.load(profile_path) if profile_path else None
        except (FileNotFoundError, ValueError):
            imported = None
        if not user and imported:
            user = self.store.create_user(self.owner, imported.name)
            self.store.update_user(user["id"], status="active", consent_at=now().isoformat())
        if user and imported and not user.get("profile_enc"):
            self.save_profile(user, imported)
            log.info("imported the owner's profile into user %s", user["id"])

    # ------------------------------------------------------------------ cost guard
    def week_spend(self, at: datetime | None = None) -> float:
        start = week_start(at or now(), self.settings.timezone)
        total = 0.0
        for row in self.store.usage_by_model_since(start):
            price_in, price_out = PRICES.get(row["model"], PRICES["claude-opus-5-5"])
            total += (row["input_tokens"] * price_in + row["output_tokens"] * price_out
                      + row["cache_read_tokens"] * price_in * 0.1) / 1_000_000
        return total

    def budget_ok(self) -> bool:
        spend = self.week_spend()
        if spend < self.settings.weekly_budget_usd:
            return True
        week = week_start(now(), self.settings.timezone).date().isoformat()
        if self.owner and self.store.get_meta("budget_alert_week") != week:
            self.store.set_meta("budget_alert_week", week)
            self.tg.send(self.owner, f"💸 This week's Claude spend (${spend:.2f}) reached the "
                                     f"${self.settings.weekly_budget_usd:.2f} budget. Scoring and drafting are paused "
                                     "until Monday. Raise WEEKLY_BUDGET_USD to continue.")
        return False

    # ------------------------------------------------------------------ schedule
    def weekly_due(self, at: datetime | None = None) -> bool:
        local = (at or now()).astimezone(self.settings.timezone)
        if self.settings.run_weekday is not None and local.weekday() != self.settings.run_weekday:
            return False
        return local.hour >= self.settings.daily_hour and self.store.get_meta("last_run") != local.date().isoformat()

    def run_weekly(self, at: datetime | None = None) -> dict:
        local = (at or now()).astimezone(self.settings.timezone)
        self.store.set_meta("last_run", local.date().isoformat())
        fetched = feed.sync(self.store, self.settings.feed_url, self.http)
        results = {}
        for user in self.store.users(("active",)):
            try:
                results[user["id"]] = self.agent_for(user).match(fetched=fetched, at=at)
            except Exception:  # one user's failure must not stop everyone else's run
                log.exception("run failed for user %s", user["id"])
                results[user["id"]] = {"error": True}
        log.info("weekly run: %d users, %d new jobs", len(results), fetched)
        return {"fetched": fetched, "users": results}

    def follow_ups_due(self, at: datetime | None = None) -> bool:
        local = (at or now()).astimezone(self.settings.timezone)
        return local.hour >= self.settings.daily_hour and self.store.get_meta("last_follow_ups") != local.date().isoformat()

    def check_follow_ups(self, at: datetime | None = None) -> int:
        local = (at or now()).astimezone(self.settings.timezone)
        self.store.set_meta("last_follow_ups", local.date().isoformat())
        total = 0
        for user in self.store.users(("active", "paused")):
            try:
                total += self.agent_for(user).send_follow_ups()
            except Exception:
                log.exception("follow-ups failed for user %s", user["id"])
        return total

    # ------------------------------------------------------------------ routing
    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
            self.on_button(chat_id, query)
        elif "message" in update:
            message = update["message"]
            chat_id = str(message.get("chat", {}).get("id", ""))
            if message.get("chat", {}).get("type", "private") != "private":
                return  # JobPilot only works in one-to-one chats
            self.on_message(chat_id, message)

    def on_message(self, chat_id: str, message: dict) -> None:
        text = (message.get("text") or "").strip()
        name = (message.get("from") or {}).get("first_name")
        if not self.owner:
            log.info("setup mode: message from chat id %s", chat_id)
            self.tg.send(chat_id, f"Setup: your chat id is <code>{esc(chat_id)}</code>. "
                                  "Set OWNER_CHAT_ID to it in .env and restart.")
            return
        command, _, arg = text.partition(" ")
        command = command.split("@")[0].lower()
        user = self.store.user_by_chat(chat_id)

        if chat_id == self.owner and command in ("/invite", "/users", "/requests"):
            return self.admin(command, arg.strip())
        if not user:
            return self.stranger(chat_id, name, command, arg.strip())
        if message.get("document"):
            return self.on_cv(user, message["document"])
        status = user["status"]
        if status == "consent" and command != "/deleteme":
            self.tg.send(chat_id, "Please tap <b>I agree</b> above to continue, or /deleteme to leave.")
        elif status == "cv" and command != "/deleteme":
            self.tg.send(chat_id, "📄 Send me your CV as a PDF to continue.")
        elif status in ("prefs", "review") and command not in ("/deleteme", "/cv"):
            self.tg.send(chat_id, "Please finish the questions above (tap a button), or send /cv to re-upload.")
        elif command == "/start":
            self.tg.send(chat_id, HELP.format(schedule=self.agent_for(user).schedule_text()))
        elif command == "/profile":
            self.tg.send(chat_id, onboarding.summary(self.profile(user)))
        elif command == "/cv":
            self.tg.send(chat_id, "📄 Send your updated CV as a PDF. Your preferences stay as they are.")
        elif command == "/prefs":
            self.ask_roles(user, flow="prefs")
        elif command == "/pause":
            self.store.update_user(user["id"], status="paused")
            self.tg.send(chat_id, "⏸ Paused. No more matches until you send /resume.")
        elif command == "/resume":
            self.store.update_user(user["id"], status="active")
            self.tg.send(chat_id, "▶️ Resumed. You'll get matches on the next run.")
        elif command == "/deleteme":
            self.tg.send(chat_id, "This permanently deletes your profile, matches and history. Are you sure?",
                         [[button("🗑 Yes, delete everything", "del:yes"), button("Cancel", "del:no")]])
        elif status in ("active", "paused"):
            self.agent_for(user).on_message(message)

    def stranger(self, chat_id: str, name: str | None, command: str, arg: str) -> None:
        if command == "/start" and arg and arg != "request":
            user = self.store.redeem_invite(arg, chat_id, name)
            if user:
                self.store.update_user(user["id"], status="consent")
                self.tg.send(chat_id, f"Welcome{', ' + esc(name) if name else ''}! 🎉\n\n{PRIVACY}",
                             [[button("✅ I agree", "consent:yes"), button("No thanks", "consent:no")]])
                return
            self.tg.send(chat_id, "That invite link isn't valid or has already been used. "
                                  "Ask whoever sent it for a new one.")
            return
        self.tg.send(chat_id, INVITE_ONLY)

    # ------------------------------------------------------------------ buttons
    def on_button(self, chat_id: str, query: dict) -> None:
        data = query.get("data", "")
        prefix = data.split(":", 1)[0]
        message_id = query.get("message", {}).get("message_id")
        if prefix == "req":  # "Request an invite" buttons left in older chats: requests are closed
            self._clear(chat_id, message_id)
            return self.tg.answer(query["id"], REQUESTS_CLOSED)
        if prefix == "adm":
            if chat_id != self.owner:
                return self.tg.answer(query["id"])
            return self.on_admin_button(query, data)
        user = self.store.user_by_chat(chat_id)
        if not user:
            return self.tg.answer(query["id"], "Please start with an invite link.")
        if prefix == "consent":
            return self.on_consent(user, query, data == "consent:yes")
        if prefix == "del":
            self.tg.answer(query["id"])
            self._clear(chat_id, message_id)
            if data == "del:yes":
                self.store.delete_user(user["id"])
                self.tg.send(chat_id, "🗑 All your data has been deleted. Thanks for trying JobPilot.")
            else:
                self.tg.send(chat_id, "Cancelled. Nothing was deleted.")
            return
        if prefix in ("pr", "pl", "ps", "pw", "rv"):
            return self.on_prefs_button(user, query, data)
        if user["status"] in ("active", "paused"):
            return self.agent_for(user).on_button(query)
        self.tg.answer(query["id"], "Please finish setting up first.")

    def on_admin_button(self, query: dict, data: str) -> None:
        _, action, chat_id = data.split(":", 2)
        request = self.store.close_request(chat_id, "approved" if action == "ok" else "declined")
        self._clear(self.owner, query.get("message", {}).get("message_id"))
        if not request:
            return self.tg.answer(query["id"], "Already handled")
        if action == "ok":
            code = self.store.create_invite(note=f"request from {request.get('name') or chat_id}")
            self.tg.send(chat_id, "🎉 You're invited to the JobPilot beta! Tap below to start.",
                         [[button("🚀 Start JobPilot", url=self.invite_link(code))]])
            self.tg.answer(query["id"], "Invite sent")
        else:
            self.tg.answer(query["id"], "Declined")

    def on_consent(self, user: dict, query: dict, agreed: bool) -> None:
        self.tg.answer(query["id"])
        self._clear(user["chat_id"], query.get("message", {}).get("message_id"))
        if not agreed:
            self.store.delete_user(user["id"])
            self.tg.send(user["chat_id"], "No problem. Nothing has been stored. Your invite link won't work again, "
                                          "so ask for a new one if you change your mind.")
            return
        self.store.update_user(user["id"], status="cv", consent_at=now().isoformat())
        self.tg.send(user["chat_id"], "Great. 📄 <b>Send me your CV as a PDF</b> (exported from Word or Google "
                                      "Docs, not a scan). I'll read it once, build your profile, and discard the file.")

    # ------------------------------------------------------------------ CV upload
    def on_cv(self, user: dict, document: dict) -> None:
        chat_id = user["chat_id"]
        if user["status"] == "consent":
            return self.tg.send(chat_id, "Please tap <b>I agree</b> first.")
        if document.get("mime_type") != "application/pdf":
            return self.tg.send(chat_id, "Please send your CV as a <b>PDF</b> file.")
        if (document.get("file_size") or 0) > onboarding.MAX_CV_BYTES:
            return self.tg.send(chat_id, "That file is over 5 MB. Please send a smaller PDF.")
        if not self.budget_ok():
            return self.tg.send(chat_id, "JobPilot has hit this week's usage limit. Please try again after Monday.")
        try:
            text = onboarding.cv_text(self.tg.download(document["file_id"], onboarding.MAX_CV_BYTES))
        except onboarding.CvError as exc:
            return self.tg.send(chat_id, f"⚠️ {esc(exc)}")
        except TelegramError:
            return self.tg.send(chat_id, "⚠️ I couldn't download that file. Please try sending it again.")
        self.tg.send(chat_id, "📖 Reading your CV… (about 30 seconds)")
        scoped = self.store.for_user(user["id"])
        try:
            draft = extract_profile(self.client, self.settings.draft_model, text, scoped.record_usage)
        except (Refused, RuntimeError, anthropic.APIError):
            log.exception("CV extraction failed for user %s", user["id"])
            return self.tg.send(chat_id, "⚠️ I couldn't process that CV just now. Please send it again in a minute.")
        existing = self.profile(user)
        profile = onboarding.profile_from_draft(draft, keep=existing)
        self.save_profile(user, profile)
        if user["status"] in ("active", "paused"):
            self.tg.send(chat_id, "✅ CV updated. Here's your profile:\n\n" + onboarding.summary(profile))
        else:
            self.store.update_user(user["id"], status="prefs")
            self.tg.send(chat_id, "✅ Got it. Here's what I found:\n\n" + onboarding.summary(profile))
            self.ask_roles(self.store.user(user["id"]), flow="onboard")

    # ------------------------------------------------------------------ preferences
    def ask_roles(self, user: dict, flow: str) -> None:
        state = {"flow": flow, "roles": []}
        if flow == "prefs":
            state["roles"] = list(self.profile(user).preferences.roles)
        self.store.update_user(user["id"], onboarding=state)
        self.tg.send(user["chat_id"], "🎯 <b>Which roles do you want?</b> Tap all that apply, then <b>Done</b>.",
                     self._role_buttons(state["roles"]))

    def _role_buttons(self, chosen: list[str]) -> list[list[dict]]:
        cells = [button(("✅ " if role in chosen else "") + role, f"pr:{i}") for i, role in enumerate(onboarding.ROLES)]
        rows = [cells[i:i + 2] for i in range(0, len(cells), 2)]
        return rows + [[button("Done ➡️", "pr:done")]]

    def on_prefs_button(self, user: dict, query: dict, data: str) -> None:
        chat_id, message_id = user["chat_id"], query.get("message", {}).get("message_id")
        state = dict(user["onboarding"] or {})
        prefix, _, value = data.partition(":")
        if prefix == "pr" and value != "done":
            role = onboarding.ROLES[int(value)] if value.isdigit() and int(value) < len(onboarding.ROLES) else None
            if role:
                roles = state.setdefault("roles", [])
                roles.remove(role) if role in roles else roles.append(role)
                self.store.update_user(user["id"], onboarding=state)
                self.tg.edit_buttons(chat_id, message_id, self._role_buttons(roles))
            return self.tg.answer(query["id"])
        self.tg.answer(query["id"])
        if prefix == "pr":
            if not state.get("roles"):
                return self.tg.send(chat_id, "Pick at least one role first.")
            self._clear(chat_id, message_id)
            return self.tg.send(chat_id, "📍 <b>Where can you work?</b>", [
                [button("🏢 On-site in Melbourne", "pl:onsite"), button("🔀 Hybrid", "pl:hybrid")],
                [button("🏠 Remote", "pl:remote"), button("Any of these", "pl:any")]])
        if prefix == "pl":
            state["locations"] = list(onboarding.LOCATIONS) if value == "any" else [value]
            self.store.update_user(user["id"], onboarding=state)
            self._clear(chat_id, message_id)
            salary_buttons = [button("Skip" if s == 0 else f"${s // 1000}k+", f"ps:{s}") for s in onboarding.SALARIES]
            return self.tg.send(chat_id, "💰 <b>Minimum salary</b> (annual, before super)?",
                                [salary_buttons[:3], salary_buttons[3:]])
        if prefix == "ps":
            state["salary"] = int(value) if value.isdigit() else 0
            self.store.update_user(user["id"], onboarding=state)
            self._clear(chat_id, message_id)
            return self.tg.send(chat_id, "🛂 <b>Your work rights in Australia?</b> (so I can skip jobs you can't take)",
                                [[button(label, f"pw:{key}")] for key, (label, *_rest) in onboarding.WORK_RIGHTS.items()])
        if prefix == "pw":
            if value not in onboarding.WORK_RIGHTS:
                return
            state["rights"] = value
            self._clear(chat_id, message_id)
            profile = onboarding.apply_preferences(self.profile(user), state)
            self.save_profile(user, profile)
            if state.get("flow") == "prefs":
                self.store.update_user(user["id"], onboarding={})
                return self.tg.send(chat_id, "✅ Preferences updated.\n\n" + onboarding.summary(profile))
            self.store.update_user(user["id"], status="review", onboarding=state)
            return self.tg.send(chat_id, "📋 <b>Here's your JobPilot profile</b>\n\n" + onboarding.summary(profile),
                                [[button("✅ Looks good", "rv:ok"), button("📄 Re-upload CV", "rv:cv")]])
        if prefix == "rv":
            self._clear(chat_id, message_id)
            if value == "cv":
                self.store.update_user(user["id"], status="cv", onboarding={})
                return self.tg.send(chat_id, "📄 Send your CV as a PDF.")
            self.store.update_user(user["id"], status="active", onboarding={})
            user = self.store.user(user["id"])
            agent = self.agent_for(user)
            self.tg.send(chat_id, "🚀 You're all set! " + HELP.format(schedule=agent.schedule_text()))
            self.tg.send(chat_id, "🔎 Finding your first matches now… (1–2 minutes)")
            try:
                fetched = feed.sync(self.store, self.settings.feed_url, self.http) if self.http else 0
                agent.match(fetched=fetched, limit=FIRST_RUN_LIMIT)
            except Exception:
                log.exception("first run failed for user %s", user["id"])
                self.tg.send(chat_id, "⚠️ I couldn't finish your first match. You'll get matches on the next run.")

    # ------------------------------------------------------------------ owner admin
    def invite_link(self, code: str) -> str:
        return f"https://t.me/{self.bot_username}?start={code}"

    def admin(self, command: str, arg: str) -> None:
        if command == "/invite":
            code = self.store.create_invite(note=arg or None)
            self.tg.send(self.owner, f"🎟 Single-use invite{' for ' + esc(arg) if arg else ''}:\n"
                                     f"{esc(self.invite_link(code))}")
        elif command == "/users":
            since = now() - timedelta(days=7)
            lines = [f"👥 <b>{len(self.store.users())} users</b> · this week ≈ ${self.week_spend():.2f} of "
                     f"${self.settings.weekly_budget_usd:.2f}"]
            for user in self.store.users():
                counts = self.store.for_user(user["id"]).counts()
                usage = self.store.usage_since(since, user_id=user["id"])
                lines.append(f"• #{user['id']} {esc(user.get('name') or '?')}: {user['status']} · sent {counts.get('sent', 0)}"
                             f" · applied {counts.get('applied', 0) + counts.get('interview', 0)}"
                             f" · interviews {counts.get('interview', 0)} · {usage['calls']} calls/7d")
            self.tg.send(self.owner, "\n".join(lines))
        elif command == "/requests":
            pending = self.store.pending_requests()
            if not pending:
                return self.tg.send(self.owner, "No pending invite requests.")
            for request in pending:
                self.tg.send(self.owner, f"🙋 {esc(request.get('name') or 'Someone')} ({request['requested_at'][:10]})",
                             [[button("✅ Approve", f"adm:ok:{request['chat_id']}"),
                               button("✖️ Decline", f"adm:no:{request['chat_id']}")]])

    def _clear(self, chat_id: str, message_id) -> None:
        if message_id:
            try:
                self.tg.edit_buttons(chat_id, message_id, None)
            except TelegramError:
                pass
