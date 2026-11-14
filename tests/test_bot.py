from io import BytesIO
from unittest.mock import patch

from cryptography.fernet import Fernet
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from agent import feed
from agent.bot import Bot
from agent.matcher import JobScore
from agent.vault import Vault
from conftest import FakeClaude, FakeTelegram, make_job

OWNER = "42"


def pdf(text: str) -> bytes:
    buffer = BytesIO()
    page = canvas.Canvas(buffer, pagesize=A4)
    y = 800
    for line in text.splitlines():
        page.drawString(40, y, line)
        y -= 14
    page.save()
    return buffer.getvalue()


CV = pdf("\n".join(["Sam Tester - Full-Stack Developer", "Experience: Developer at Acme 2021-2025"] +
                   [f"Built React dashboards and FastAPI services, project {n}." for n in range(12)]))
SCAN = pdf("")


def msg(chat, text="", **extra):
    return {"message": {"chat": {"id": int(chat), "type": "private"}, "from": {"first_name": "Sam"}, "text": text, **extra}}


def btn(chat, data, message_id=9):
    return {"callback_query": {"id": "cb", "data": data, "from": {"first_name": "Sam", "username": "sam"},
                               "message": {"message_id": message_id, "chat": {"id": int(chat)}}}}


@pytest.fixture
def bot(settings, store):
    tg = FakeTelegram()
    tg.files = {"cv": CV, "scan": SCAN}
    return Bot(settings, store, tg, FakeClaude(), http=None, vault=Vault(Fernet.generate_key().decode()),
               bot_username="test_bot")


def onboard(bot, chat="100", rights="visa"):
    """Owner invites, the user goes through the whole flow."""
    bot.handle(msg(OWNER, "/invite Sam"))
    code = bot.tg.to(OWNER)[-1]["text"].split("start=")[1].strip()
    bot.handle(msg(chat, f"/start {code}"))
    bot.handle(btn(chat, "consent:yes"))
    bot.handle(msg(chat, document={"file_id": "cv", "mime_type": "application/pdf", "file_size": len(CV)}))
    bot.handle(btn(chat, "pr:0"))
    bot.handle(btn(chat, "pr:1"))
    bot.handle(btn(chat, "pr:done"))
    bot.handle(btn(chat, "pl:hybrid"))
    bot.handle(btn(chat, "ps:130000"))
    bot.handle(btn(chat, f"pw:{rights}"))
    return bot.store.user_by_chat(chat)


def test_setup_mode_replies_with_chat_id(bot):
    bot.settings = type(bot.settings)(**{**bot.settings.__dict__, "telegram_chat_id": ""})
    bot.handle(msg("123", "/start"))
    assert "123" in bot.tg.to("123")[-1]["text"]


def test_strangers_get_no_request_button(bot):
    for text in ("/start", "/start request", "hello"):
        bot.handle(msg("100", text))
        reply = bot.tg.to("100")[-1]
        assert "invite-only" in reply["text"] and "jobpilot.imisstech.com.au" in reply["text"]
        assert reply["buttons"] == []
    bot.handle(msg("100", "/start not-a-real-code"))
    reply = bot.tg.to("100")[-1]
    assert "isn't valid" in reply["text"] and reply["buttons"] == []
    bot.handle(btn("100", "req:new"))                 # a button left in an older chat stores nothing
    assert bot.store.pending_requests() == []
    assert not any("Invite request" in m["text"] for m in bot.tg.to(OWNER))
    assert "personal" in bot.tg.answers[-1]


def test_owner_can_still_answer_earlier_requests(bot):
    bot.store.request_invite("100", "Sam")            # made before requests were closed
    bot.handle(msg(OWNER, "/requests"))
    assert "adm:ok:100" in [b["callback_data"] for row in bot.tg.to(OWNER)[-1]["buttons"] for b in row]
    bot.handle(btn("999", "adm:ok:100"))              # only the owner can approve
    assert not any("You're invited" in m["text"] for m in bot.tg.to("100"))
    bot.handle(btn(OWNER, "adm:ok:100"))
    invite = bot.tg.to("100")[-1]
    assert "You're invited" in invite["text"] and invite["buttons"][0][0]["url"].startswith("https://t.me/test_bot?start=")
    assert bot.store.pending_requests() == []


def test_invite_link_is_single_use(bot):
    onboard(bot, "100")
    code = bot.store.user_by_chat("100")["invite_code"]
    bot.handle(msg("200", f"/start {code}"))
    assert "isn't valid" in bot.tg.to("200")[-1]["text"]
    assert bot.store.user_by_chat("200") is None


def test_full_onboarding_builds_an_encrypted_profile(bot):
    with patch.object(feed, "sync", return_value=0):
        user = onboard(bot)
        assert user["status"] == "review"
        assert b"Sam Tester" not in user["profile_enc"]   # encrypted at rest
        profile = bot.profile(user)
        assert profile.name == "Sam Tester" and profile.contact["email"] == "sam@example.com"
        assert profile.preferences.roles == ["Software Engineer", "Full-Stack Developer"]
        assert profile.preferences.locations == ["Melbourne (hybrid)"] and profile.preferences.min_salary == 130000
        assert "security clearance" in profile.preferences.avoid_description_phrases
        assert profile.screening["Work rights"].startswith("Full working rights")
        assert profile.screening["Salary expectation"] == "From $130,000 + super, depending on the role"
        bot.store.save_jobs([make_job(1, title="React Developer", skills=("React",))])
        bot.handle(btn("100", "rv:ok"))
    assert bot.store.user_by_chat("100")["status"] == "active"
    texts = [m["text"] for m in bot.tg.to("100")]
    assert any("You're all set" in t for t in texts)
    assert any("80/100" in t for t in texts)          # first match arrived
    assert bot.store.user_by_chat("100")["onboarding"] == {}


def test_cv_problems_get_clear_replies(bot):
    bot.handle(msg(OWNER, "/invite"))
    code = bot.tg.to(OWNER)[-1]["text"].split("start=")[1].strip()
    bot.handle(msg("100", f"/start {code}"))
    bot.handle(msg("100", document={"file_id": "cv", "mime_type": "application/pdf"}))
    assert "I agree" in bot.tg.to("100")[-1]["text"]                  # consent first
    bot.handle(btn("100", "consent:yes"))
    bot.handle(msg("100", document={"file_id": "x", "mime_type": "image/png"}))
    assert "PDF" in bot.tg.to("100")[-1]["text"]
    bot.handle(msg("100", document={"file_id": "scan", "mime_type": "application/pdf", "file_size": 10}))
    assert "scan" in bot.tg.to("100")[-1]["text"]
    bot.handle(msg("100", document={"file_id": "cv", "mime_type": "application/pdf", "file_size": 6 * 1024 * 1024}))
    assert "5 MB" in bot.tg.to("100")[-1]["text"]
    assert bot.store.user_by_chat("100")["status"] == "cv"


def test_declining_consent_stores_nothing(bot):
    bot.handle(msg(OWNER, "/invite"))
    code = bot.tg.to(OWNER)[-1]["text"].split("start=")[1].strip()
    bot.handle(msg("100", f"/start {code}"))
    bot.handle(btn("100", "consent:no"))
    assert bot.store.user_by_chat("100") is None


def activate(bot, chat):
    with patch.object(feed, "sync", return_value=0):
        onboard(bot, chat)
        bot.handle(btn(chat, "rv:ok"))
    return bot.store.user_by_chat(chat)


def test_users_are_isolated(bot):
    a, b = activate(bot, "100"), activate(bot, "200")
    bot.store.save_jobs([make_job(5)])
    a_store = bot.store.for_user(a["id"])
    a_store.save_score(make_job(5)["id"], JobScore(job_id=make_job(5)["id"], score=90, verdict="strong",
                                                   reasons=[], gaps=[], red_flags=[]), "m")
    job_id = make_job(5)["id"]
    bot.handle(btn("200", f"approve:{job_id}"))      # B presses A's button data
    assert bot.tg.answers[-1] == "That job is no longer tracked."
    assert a_store.match(job_id)["status"] == "scored"
    assert bot.store.for_user(b["id"]).match(job_id) is None
    bot.handle(btn("100", f"approve:{job_id}"))      # A can
    assert a_store.match(job_id)["status"] == "approved"
    assert any("Application kit 1" in m["text"] for m in bot.tg.to("100"))
    assert not any("Application kit" in m["text"] for m in bot.tg.to("200"))
    assert bot.tg.documents and all(d["filename"].startswith("Sam_Tester") for d in bot.tg.documents)


def test_deleteme_removes_all_their_data(bot):
    user = activate(bot, "100")
    bot.handle(msg("100", "/deleteme"))
    bot.handle(btn("100", "del:yes"))
    assert bot.store.user_by_chat("100") is None
    for table in ("matches", "api_usage"):
        assert bot.store.db.execute(f"SELECT count(*) FROM {table} WHERE user_id = ?", (user["id"],)).fetchone()[0] == 0
    assert "deleted" in bot.tg.to("100")[-1]["text"]


def test_pause_resume_and_weekly_run_covers_active_users_only(bot):
    activate(bot, "100")
    activate(bot, "200")
    bot.handle(msg("200", "/pause"))
    bot.store.save_jobs([make_job(7)])
    with patch.object(feed, "sync", return_value=1):
        result = bot.run_weekly()
    ran = set(result["users"])
    assert bot.store.user_by_chat("100")["id"] in ran and bot.store.user_by_chat("200")["id"] not in ran
    bot.handle(msg("200", "/resume"))
    assert bot.store.user_by_chat("200")["status"] == "active"


def test_budget_stops_scoring_and_alerts_owner_once(bot):
    user = activate(bot, "100")
    bot.settings = type(bot.settings)(**{**bot.settings.__dict__, "weekly_budget_usd": 0.01})
    bot.store.save_jobs([make_job(n) for n in range(10, 14)])
    with patch.object(feed, "sync", return_value=4):
        result = bot.run_weekly()
        bot.run_weekly()
    assert "budget" in result["users"][user["id"]]["problem"]
    assert len([m for m in bot.tg.to(OWNER) if "budget" in m["text"]]) == 1


def test_owner_admin_commands_are_owner_only(bot):
    activate(bot, "100")
    bot.handle(msg("100", "/invite"))
    assert not any("invite" in m["text"].lower() and "t.me" in m["text"] for m in bot.tg.to("100"))
    bot.handle(msg(OWNER, "/users"))
    assert "Sam" in bot.tg.to(OWNER)[-1]["text"] and "active" in bot.tg.to(OWNER)[-1]["text"]


def test_ensure_owner_imports_profile(bot, tmp_path):
    from conftest import ROOT
    bot.ensure_owner(str(ROOT / "profile.example.yaml"))
    owner = bot.store.user_by_chat(OWNER)
    assert owner["status"] == "active" and bot.profile(owner).name == "Alex Example"


def test_group_chats_are_ignored(bot):
    bot.handle({"message": {"chat": {"id": -5, "type": "group"}, "text": "/start"}})
    assert bot.tg.sent == []
