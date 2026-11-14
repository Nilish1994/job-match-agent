"""One user's agent: their matching run plus their Telegram approval handlers.

`agent.bot.Bot` routes each chat to the right user's Agent. Nothing is ever
submitted to an employer: approving a match drafts the application, the
candidate applies on the employer's site and taps "Applied".
"""
from datetime import datetime, timedelta
import logging

import anthropic
import httpx

from agent import documents, feed, prefilter
from agent.config import Settings
from agent.matcher import Matcher, Refused
from agent.store import UserStore, now
from agent.telegram import Telegram, TelegramError, button, esc

log = logging.getLogger("agent")

# USD per million tokens: (input, output). Used only for the /stats estimate.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
VERDICT_ICON = {"strong": "🟢", "possible": "🟡", "stretch": "🟠", "no": "⚪"}

HELP = (
    "<b>JobPilot</b>\n"
    "On schedule ({schedule}) I score new Melbourne tech jobs against your profile and send the best matches.\n\n"
    "• <b>Approve</b> sends an application kit: tailored CV and cover letter PDFs plus paste-ready answers\n"
    "• <b>Improve with full ad</b>: paste the whole advert for a sharper letter\n"
    "• Apply on the employer's site yourself, then tap <b>Applied</b>\n"
    "• I'll check in a week later to record the outcome\n\n"
    "/run: match now · /pending: applications in progress · /stats: your pipeline\n"
    "/profile · /cv: update your CV · /prefs: change preferences · /pause · /resume · /deleteme"
)


def salary_text(job: dict) -> str:
    low, high = job.get("salary_min"), job.get("salary_max")
    fmt = lambda v: f"${round(v / 1000)}k" if v and v >= 1000 else None
    low, high = fmt(low), fmt(high)
    if low and high and low != high:
        return f"{low}–{high}"
    return low or high or ""


class Agent:
    def __init__(self, settings: Settings, store: UserStore, telegram: Telegram, matcher: Matcher, http: httpx.Client,
                 chat_id: str, budget_ok=None):
        self.settings, self.store, self.tg, self.matcher, self.http = settings, store, telegram, matcher, http
        self.chat_id = str(chat_id)
        # Shared spending guard set by the Bot; None means no limit (tests, single user).
        self.budget_ok = budget_ok or (lambda: True)

    @property
    def chat(self) -> str:
        return self.chat_id

    # ---------------------------------------------------------------- daily run
    def daily_due(self, at: datetime | None = None) -> bool:
        """True once per scheduled day, from DAILY_HOUR (local time) onwards."""
        local = (at or now()).astimezone(self.settings.timezone)
        if self.settings.run_weekday is not None and local.weekday() != self.settings.run_weekday:
            return False
        return local.hour >= self.settings.daily_hour and self.store.get_meta("last_daily_run") != local.date().isoformat()

    def follow_ups_due(self, at: datetime | None = None) -> bool:
        """Follow-up reminders are checked every day, even when matching runs weekly."""
        local = (at or now()).astimezone(self.settings.timezone)
        return local.hour >= self.settings.daily_hour and self.store.get_meta("last_follow_up_check") != local.date().isoformat()

    def check_follow_ups(self, at: datetime | None = None) -> int:
        local = (at or now()).astimezone(self.settings.timezone)
        self.store.set_meta("last_follow_up_check", local.date().isoformat())
        return self.send_follow_ups()

    def schedule_text(self) -> str:
        hour = f"{self.settings.daily_hour % 12 or 12}{'am' if self.settings.daily_hour < 12 else 'pm'}"
        if self.settings.run_weekday is None:
            return f"every day at {hour}"
        from agent.config import WEEKDAYS
        return f"every {WEEKDAYS[self.settings.run_weekday].title()} at {hour}"

    def run_daily(self, at: datetime | None = None) -> dict:
        local = (at or now()).astimezone(self.settings.timezone)
        self.store.set_meta("last_daily_run", local.date().isoformat())
        fetched = feed.sync(self.store.root, self.settings.feed_url, self.http)
        return self.match(fetched=fetched, at=at)

    def match(self, fetched: int, at: datetime | None = None, limit: int | None = None) -> dict:
        """Score this user's unscored recent jobs and send their digest (the feed is synced by the caller)."""
        fresh = self.store.unscored_jobs(since=now() - timedelta(days=self.settings.max_job_age_days))
        to_score = prefilter.select(fresh, self.matcher.profile, limit or self.settings.max_scored_per_run)
        scored, problem = self.score(to_score)
        sent = self.send_digest(fetched=fetched, scored=scored, problem=problem)
        reminders = self.check_follow_ups(at)
        summary = {"fetched": fetched, "candidates": len(to_score), "scored": scored, "sent": sent,
                   "follow_ups": reminders, "problem": problem}
        log.info("run for user %s: %s", self.store.user_id, summary)
        return summary

    def score(self, jobs: list[dict]) -> tuple[int, str | None]:
        scored, size = 0, self.settings.score_batch
        for start in range(0, len(jobs), size):
            if not self.budget_ok():
                return scored, "This week's matching budget is used up; the rest will be scored next run."
            batch = jobs[start:start + size]
            try:
                results = self.matcher.score(batch)
            except Refused:
                log.warning("scoring batch declined; skipping %d jobs", len(batch))
                continue
            except anthropic.RateLimitError:
                return scored, "Claude rate limit reached; the rest will be scored next run."
            except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
                log.error("scoring failed: %s", type(exc).__name__)
                return scored, "Claude was unavailable; the rest will be scored next run."
            with self.store.transaction():
                for result in results:
                    self.store.save_score(result.job_id, result, self.settings.score_model)
            scored += len(results)
        return scored, None

    def send_digest(self, fetched: int, scored: int, problem: str | None) -> int:
        matches = self.store.digest_candidates(self.settings.min_score, self.settings.max_digest)
        if not matches:
            note = f"No new matches above {self.settings.min_score} today ({fetched} new jobs, {scored} scored)."
            self.tg.send(self.chat, note + (f"\n⚠️ {esc(problem)}" if problem else ""))
            return 0
        header = f"☀️ <b>{len(matches)} job match{'es' if len(matches) > 1 else ''}</b> from {fetched} new jobs ({scored} scored)."
        if problem:
            header += f"\n⚠️ {esc(problem)}"
        self.tg.send(self.chat, header)
        for match in matches:
            self.tg.send(self.chat, self.card(match), self.card_buttons(match))
            self.store.set_status(match["job_id"], "sent")
        return len(matches)

    def send_follow_ups(self) -> int:
        due = self.store.due_follow_ups()
        for match in due:
            job_id = match["job_id"]
            self.tg.send(self.chat, f"📬 Any news from <b>{esc(match['company'] or 'the employer')}</b> about "
                                    f"<b>{esc(match['title'])}</b>?", [
                [button("🎉 Interview", f"out:{job_id}:interview"), button("❌ Rejected", f"out:{job_id}:rejected")],
                [button("🔇 No reply", f"out:{job_id}:no_reply"), button("⏰ Ask later", f"out:{job_id}:later")]])
            # Re-ask in 3 days if there's no answer.
            self.store.snooze_follow_up(job_id, 3)
        return len(due)

    # ---------------------------------------------------------------- messages
    def card(self, match: dict) -> str:
        lines = [f"{VERDICT_ICON.get(match['verdict'], '')} <b>{match['score']}/100</b> · <b>{esc(match['title'])}</b>",
                 " · ".join(filter(None, [esc(match.get("company")), esc(match.get("location")), salary_text(match)]))]
        lines += [f"✅ {esc(r)}" for r in match["reasons"][:3]]
        lines += [f"⚠️ {esc(g)}" for g in match["gaps"][:3]]
        lines += [f"🚩 {esc(f)}" for f in match["red_flags"][:3]]
        return "\n".join(lines)

    def card_buttons(self, match: dict) -> list[list[dict]]:
        job_id = match["job_id"]
        rows = [[button("✅ Approve & draft", f"approve:{job_id}"), button("✖️ Skip", f"skip:{job_id}")]]
        if match.get("url"):
            rows.append([button("🔗 Open advert", url=match["url"])])
        return rows

    def letter_buttons(self, job: dict) -> list[list[dict]]:
        job_id = job["id"]
        rows = [[button("📨 Applied", f"applied:{job_id}"), button("📋 Improve with full ad", f"fullad:{job_id}")],
                [button("🔁 Rewrite", f"redo:{job_id}"), button("✖️ Drop", f"skip:{job_id}")]]
        if job.get("url"):
            rows.insert(0, [button("🔗 Open advert to apply", url=job["url"])])
        return rows

    def draft(self, job_id: str, full_advert: str | None = None) -> None:
        job = self.store.job(job_id)
        if not job:
            self.tg.send(self.chat, "I can't find that job any more.")
            return
        today = (now().astimezone(self.settings.timezone)).replace(hour=0, minute=0, second=0, microsecond=0)
        if self.store.drafts_since(today) >= self.settings.max_drafts_per_day:
            self.tg.send(self.chat, f"You've reached today's limit of {self.settings.max_drafts_per_day} drafts. "
                                    "Tap <b>Rewrite</b> tomorrow.", [[button("🔁 Rewrite", f"redo:{job_id}")]])
            return
        if not self.budget_ok():
            self.tg.send(self.chat, "Drafting is paused for this week's budget. Please try again after Sunday.")
            return
        try:
            result = self.matcher.letter(job, full_advert)
        except Refused:
            self.tg.send(self.chat, "Claude declined to draft this one. Try <b>Improve with full ad</b> or write it yourself.")
            return
        except anthropic.RateLimitError:
            self.tg.send(self.chat, "Claude is rate-limited right now. Tap <b>Rewrite</b> in a minute.",
                         [[button("🔁 Rewrite", f"redo:{job_id}")]])
            return
        except (anthropic.APIStatusError, anthropic.APIConnectionError):
            self.tg.send(self.chat, "Claude is unavailable right now. Tap <b>Rewrite</b> to try again.",
                         [[button("🔁 Rewrite", f"redo:{job_id}")]])
            return
        notes = {"highlights": result.highlights, "cv_tweaks": result.cv_tweaks, "check": result.check_before_sending,
                 "cv_summary": result.cv_summary, "lead_skills": result.lead_skills,
                 "featured_projects": result.featured_projects, "why_this_role": result.why_this_role}
        version = self.store.save_letter(job_id, result.cover_letter, notes)
        text = [f"✍️ <b>Application kit {version}: {esc(job['title'])}</b> at {esc(job.get('company') or '')}", "",
                f"<pre>{esc(result.cover_letter)}</pre>"]
        if result.check_before_sending:
            text += ["", "<b>Check before sending</b>"] + [f"• {esc(c)}" for c in result.check_before_sending]
        self.tg.send(self.chat, "\n".join(text))
        self.send_kit(job, result)

    def send_kit(self, job: dict, result) -> None:
        """Tailored CV + letter PDFs and paste-ready answers, ending with the action buttons."""
        profile = self.matcher.profile
        stem = documents.file_stem(profile, job)
        try:
            self.tg.send_document(self.chat, f"{stem}_CV.pdf", documents.cv_pdf(profile, result),
                                  caption="📄 CV tailored for this role")
            self.tg.send_document(self.chat, f"{stem}_Cover_Letter.pdf", documents.letter_pdf(profile, result, job),
                                  caption="📄 Cover letter")
        except TelegramError as exc:
            log.error("sending documents failed: %s", exc)
            self.tg.send(self.chat, "⚠️ I couldn't send the PDFs. The letter text above still works.")
        lines = ["📋 <b>Paste-ready answers</b> (tap a block to copy)"]
        for question, answer in documents.screening_text(profile, result):
            lines += ["", f"<b>{esc(question)}</b>", f"<code>{esc(answer)}</code>"]
        if not profile.contact.get("email") or not profile.contact.get("phone"):
            lines += ["", "ℹ️ Your CV has no email or phone yet. Send /cv with a CV that includes them."]
        lines += ["", "Open the advert, apply with the PDFs, then tap <b>Applied</b>."]
        self.tg.send(self.chat, "\n".join(lines), self.letter_buttons(job))

    # ---------------------------------------------------------------- updates
    def handle(self, update: dict) -> None:
        if "callback_query" in update:
            self.on_button(update["callback_query"])
        elif "message" in update:
            self.on_message(update["message"])

    def authorised(self, chat_id) -> bool:
        return bool(self.chat) and str(chat_id) == self.chat

    def on_message(self, message: dict) -> None:
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()
        if not self.authorised(chat_id):
            return
        waiting = self.store.pop_input(chat_id)
        if waiting and text and not text.startswith("/"):
            self.tg.send(chat_id, "Rewriting with the full advert…")
            self.draft(waiting["job_id"], full_advert=text)
            return
        command = text.split()[0].split("@")[0].lower() if text else ""
        if command in ("/start", "/help"):
            self.tg.send(chat_id, HELP.format(schedule=self.schedule_text()))
        elif command == "/run":
            today = now().astimezone(self.settings.timezone).date().isoformat()
            if self.store.get_meta("last_manual_run") == today:
                self.tg.send(chat_id, "You've already run a match today. Your next one is automatic "
                                      f"({self.schedule_text()}).")
                return
            self.store.set_meta("last_manual_run", today)
            self.tg.send(chat_id, "Matching now. This takes a minute or two.")
            self.run_daily()
        elif command == "/pending":
            self.tg.send(chat_id, self.pending_text())
        elif command == "/stats":
            self.tg.send(chat_id, self.stats_text())
        else:
            self.tg.send(chat_id, "I didn't catch that. /help lists what I can do.")

    def on_button(self, query: dict) -> None:
        chat_id = str(query.get("message", {}).get("chat", {}).get("id", ""))
        if not self.authorised(chat_id):
            return
        data = query.get("data", "")
        action, _, rest = data.partition(":")
        job_id, _, arg = rest.partition(":")
        message_id = query.get("message", {}).get("message_id")
        match = self.store.match(job_id) if job_id else None
        if not match:
            self.tg.answer(query["id"], "That job is no longer tracked.")
            return
        if action == "approve":
            self.tg.answer(query["id"], "Drafting your application…")
            self.store.set_status(job_id, "approved")
            self._clear(chat_id, message_id)
            self.draft(job_id)
        elif action == "redo":
            self.tg.answer(query["id"], "Rewriting…")
            self.draft(job_id)
        elif action == "fullad":
            self.tg.answer(query["id"])
            self.store.await_input(chat_id, job_id, "fullad")
            self.tg.send(chat_id, "Paste the full advert text as your next message and I'll rewrite the letter.")
        elif action == "skip":
            self.store.set_status(job_id, "skipped")
            self.tg.answer(query["id"], "Skipped")
            self._clear(chat_id, message_id)
        elif action == "applied":
            self.store.mark_applied(job_id, self.settings.follow_up_days)
            self.tg.answer(query["id"], "Logged as applied")
            self._clear(chat_id, message_id)
            self.tg.send(chat_id, f"📨 Logged. I'll check in about <b>{esc(self.store.job(job_id)['title'])}</b> "
                                  f"in {self.settings.follow_up_days} days.")
        elif action == "out" and arg in ("interview", "rejected", "no_reply"):
            self.store.set_status(job_id, arg)
            self.tg.answer(query["id"], {"interview": "Great news! 🎉", "rejected": "Noted. Onwards.",
                                         "no_reply": "Noted"}[arg])
            self._clear(chat_id, message_id)
        elif action == "out" and arg == "later":
            self.store.snooze_follow_up(job_id, self.settings.follow_up_days)
            self.tg.answer(query["id"], f"I'll ask again in {self.settings.follow_up_days} days")
            self._clear(chat_id, message_id)
        else:
            self.tg.answer(query["id"], "Unknown action")

    def _clear(self, chat_id: str, message_id) -> None:
        if message_id:
            try:
                self.tg.edit_buttons(chat_id, message_id, None)
            except TelegramError:
                pass  # e.g. message too old to edit; the action itself already succeeded

    def pending_text(self) -> str:
        approved = self.store.pipeline(("approved",))
        applied = self.store.pipeline(("applied",))
        if not approved and not applied:
            return "Nothing in progress. Approve a match to start an application."
        lines = []
        if approved:
            lines += ["<b>Drafted, not applied yet</b>"] + [f"• {esc(m['title'])} at {esc(m['company'])}" for m in approved]
        if applied:
            lines += ["", "<b>Applied, waiting to hear</b>"] + [f"• {esc(m['title'])} at {esc(m['company'])}" for m in applied]
        return "\n".join(lines).strip()

    def stats_text(self) -> str:
        counts = self.store.counts()
        order = ["sent", "approved", "applied", "interview", "rejected", "no_reply", "skipped"]
        lines = ["<b>Pipeline</b>"] + [f"• {status.replace('_', ' ')}: {counts.get(status, 0)}" for status in order]
        usage = self.store.usage_since(now() - timedelta(days=30))
        price_in, price_out = PRICES.get(self.settings.score_model, PRICES["claude-opus-5-5"])
        cost = (usage["input_tokens"] * price_in + usage["output_tokens"] * price_out
                + usage["cache_read_tokens"] * price_in * 0.1) / 1_000_000
        lines += ["", f"<b>Claude, last 30 days</b>: {usage['calls']} calls, ≈ ${cost:.2f}"]
        return "\n".join(lines)
