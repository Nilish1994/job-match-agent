from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import anthropic
import httpx
import pytest

from agent import feed
from agent.matcher import JobScore
from conftest import make_job

MELB_9AM = datetime(2026, 11, 16, 22, 0, tzinfo=timezone.utc)  # 9am Tuesday in Melbourne (AEDT)


def callback(data, chat_id="42", message_id=7):
    return {"callback_query": {"id": "cb", "data": data,
                               "message": {"message_id": message_id, "chat": {"id": int(chat_id)}}}}


def message(text, chat_id="42"):
    return {"message": {"text": text, "chat": {"id": int(chat_id)}}}


@pytest.fixture
def seeded(agent):
    """Feed returns 3 jobs; the fake Claude scores them 90 / 60 / 85."""
    jobs = [make_job(1, title="Python Developer"), make_job(2, title="Frontend Developer"),
            make_job(3, title="AI Engineer")]
    scores = {jobs[0]["id"]: 90, jobs[1]["id"]: 60, jobs[2]["id"]: 85}
    agent.matcher.client.scorer = lambda ids: [
        JobScore(job_id=i, score=scores[i], verdict="strong" if scores[i] >= 85 else "stretch",
                 reasons=["Matches FastAPI"], gaps=["No Kubernetes"], red_flags=[]) for i in ids]
    with patch.object(feed, "sync", side_effect=lambda store, url, http: store.save_jobs(jobs)):
        summary = agent.run_daily(at=MELB_9AM)
    return agent, jobs, summary


def test_daily_run_sends_only_matches_above_threshold(seeded):
    agent, jobs, summary = seeded
    assert summary == {"fetched": 3, "candidates": 3, "scored": 3, "sent": 2, "follow_ups": 0, "problem": None}
    texts = [m["text"] for m in agent.tg.sent]
    assert "2 job matches" in texts[0]
    assert "90/100" in texts[1] and "85/100" in texts[2]  # best first
    assert f"approve:{jobs[0]['id']}" in agent.tg.callbacks(1)
    assert agent.store.match(jobs[0]["id"])["status"] == "sent"
    assert agent.store.match(jobs[1]["id"])["status"] == "scored"  # below threshold, never sent
    assert not agent.daily_due(at=MELB_9AM + timedelta(hours=2))  # once per day


def test_relisted_jobs_are_sent_once(agent):
    jobs = [make_job(1, title="Full Stack Developer"), make_job(2, title="Full Stack Developer "),
            make_job(3, title="AI Engineer")]
    jobs[1]["company_name"] = jobs[0]["company_name"]  # same role, listed twice
    with patch.object(feed, "sync", side_effect=lambda store, url, http: store.save_jobs(jobs)):
        summary = agent.run_daily(at=MELB_9AM)
    assert summary["sent"] == 2
    statuses = sorted(agent.store.match(j["id"])["status"] for j in jobs)
    assert statuses == ["sent", "sent", "skipped"]
    # A later re-listing of a role already sent is skipped too.
    later = make_job(4, title="AI Engineer")
    later["company_name"] = jobs[2]["company_name"]
    agent.store.save_jobs([later])
    agent.store.save_score(later["id"], JobScore(job_id=later["id"], score=95, verdict="strong", reasons=[],
                                                 gaps=[], red_flags=[]), "m")
    assert agent.store.digest_candidates(70, 5) == []
    assert agent.store.match(later["id"])["status"] == "skipped"


def test_daily_due_waits_for_the_hour(agent):
    assert not agent.daily_due(at=datetime(2026, 11, 16, 19, 0, tzinfo=timezone.utc))  # 6am Melbourne
    assert agent.daily_due(at=MELB_9AM)


def test_approve_drafts_letter_then_applied_then_follow_up(seeded):
    agent, jobs, _ = seeded
    job_id = jobs[0]["id"]
    agent.handle(callback(f"approve:{job_id}"))
    assert agent.store.match(job_id)["status"] == "approved"
    letter = agent.tg.sent[-1]
    assert "Draft 1" in letter["text"] and "Dear team" in letter["text"] and "Start date" in letter["text"]
    assert f"applied:{job_id}" in agent.tg.callbacks()
    assert any(b.get("url") == jobs[0]["source_url"] for row in letter["buttons"] for b in row)

    agent.handle(callback(f"applied:{job_id}"))
    match = agent.store.match(job_id)
    assert match["status"] == "applied" and match["follow_up_at"]

    agent.store.db.execute("UPDATE matches SET follow_up_at = ? WHERE job_id = ?",
                           ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), job_id))
    assert agent.send_follow_ups() == 1
    assert f"out:{job_id}:interview" in agent.tg.callbacks()
    assert agent.send_follow_ups() == 0  # snoozed, not repeated straight away
    agent.handle(callback(f"out:{job_id}:interview"))
    assert agent.store.match(job_id)["status"] == "interview"


def test_full_advert_rewrite(seeded):
    agent, jobs, _ = seeded
    job_id = jobs[2]["id"]
    agent.handle(callback(f"approve:{job_id}"))
    agent.handle(callback(f"fullad:{job_id}"))
    agent.handle(message("Full advert: we want LangChain and AWS."))
    assert "Full advert: we want LangChain and AWS." in agent.matcher.client.calls[-1]["messages"][0]["content"]
    assert "Draft 2" in agent.tg.sent[-1]["text"]


def test_skip(seeded):
    agent, jobs, _ = seeded
    agent.handle(callback(f"skip:{jobs[0]['id']}"))
    assert agent.store.match(jobs[0]["id"])["status"] == "skipped"
    assert agent.tg.cleared == [7]


def test_other_chats_are_ignored(seeded):
    agent, jobs, _ = seeded
    before = len(agent.tg.sent)
    agent.handle(callback(f"approve:{jobs[0]['id']}", chat_id="999"))
    agent.handle(message("/run", chat_id="999"))
    assert len(agent.tg.sent) == before
    assert agent.store.match(jobs[0]["id"])["status"] == "sent"


def test_setup_mode_replies_with_chat_id(agent):
    agent.settings = type(agent.settings)(**{**agent.settings.__dict__, "telegram_chat_id": ""})
    agent.handle(message("/start", chat_id="123"))
    assert "123" in agent.tg.sent[-1]["text"] and agent.tg.sent[-1]["chat_id"] == "123"


def test_rate_limit_while_scoring_is_reported_not_fatal(agent):
    response = httpx.Response(429, request=httpx.Request("POST", "https://api.anthropic.com"))
    def limited(**kwargs):
        raise anthropic.RateLimitError("slow down", response=response, body=None)
    agent.matcher.client.beta.messages.parse = limited
    with patch.object(feed, "sync", side_effect=lambda store, url, http: store.save_jobs([make_job(1)])):
        summary = agent.run_daily(at=MELB_9AM)
    assert summary["scored"] == 0 and "rate limit" in summary["problem"]
    assert "rate limit" in agent.tg.sent[-1]["text"]


def test_commands(seeded):
    agent, jobs, _ = seeded
    agent.handle(callback(f"approve:{jobs[0]['id']}"))
    agent.handle(message("/pending"))
    assert "Python Developer" in agent.tg.sent[-1]["text"]
    agent.handle(message("/stats"))
    assert "approved: 1" in agent.tg.sent[-1]["text"] and "calls" in agent.tg.sent[-1]["text"]
    agent.handle(message("/help"))
    assert "Approve" in agent.tg.sent[-1]["text"]


def test_card_escapes_html(agent, store):
    card = agent.card({"job_id": "x", "score": 80, "verdict": "possible", "title": "<script>", "company": "A & B",
                       "location": None, "salary_min": None, "salary_max": None, "reasons": ["<b>"], "gaps": [],
                       "red_flags": []})
    assert "&lt;script&gt;" in card and "A &amp; B" in card and "&lt;b&gt;" in card
