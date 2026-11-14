from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.app import Agent
from agent.config import Settings
from agent.matcher import CoverLetter, JobScore, Matcher
from agent.profile import Profile
from agent.store import Store

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def profile():
    return Profile.load(ROOT / "profile.example.yaml")


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "agent.db"))


def make_job(n, *, title="Python Developer", skills=("Python", "FastAPI"), days_ago=1, **extra):
    seen = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return {"id": f"00000000-0000-0000-0000-{n:012d}", "title": title, "company_name": f"Company {n}",
            "location": "Melbourne CBD, Melbourne", "description": f"Build APIs with {', '.join(skills)}.",
            "salary_min": "110000", "salary_max": "130000", "source_url": f"https://example.com/jobs/{n}",
            "posted_at": seen.isoformat(), "first_seen_at": seen.isoformat(), "skills": list(skills), **extra}


class FakeTelegram:
    def __init__(self):
        self.sent, self.answers, self.cleared, self.documents = [], [], [], []

    def send(self, chat_id, text, buttons=None):
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons or []})
        return {"message_id": len(self.sent)}

    def answer(self, callback_id, text=""):
        self.answers.append(text)

    def send_document(self, chat_id, filename, content, caption="", mime="application/pdf"):
        self.documents.append({"filename": filename, "content": content, "caption": caption})

    def edit_buttons(self, chat_id, message_id, buttons):
        self.cleared.append(message_id)

    def updates(self, offset, wait=25):
        return []

    def callbacks(self, message_index=-1):
        return [b["callback_data"] for row in self.sent[message_index]["buttons"] for b in row if "callback_data" in b]


class FakeClaude:
    """Stands in for anthropic.Anthropic(); returns scripted parse results."""

    def __init__(self, scorer=None, letter=None):
        self.calls = []
        self.scorer = scorer or (lambda job_ids: [JobScore(job_id=j, score=80, verdict="possible",
                                                           reasons=["Python match"], gaps=[], red_flags=[])
                                                  for j in job_ids])
        self.letter = letter or (lambda: CoverLetter(
            cover_letter="Dear team,\n\nI'd love to join you.\n\nAlex Example", highlights=["Project A"],
            cv_tweaks=[], check_before_sending=["Start date"],
            cv_summary="Full-stack engineer with React and FastAPI experience.",
            lead_skills=["FastAPI", "Kubernetes", "python"], featured_projects=["example-dashboard", "Made Up"],
            why_this_role="Your API platform matches the services I build."))
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self.parse))

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        usage = SimpleNamespace(input_tokens=1000, output_tokens=200, cache_read_input_tokens=0)
        if kwargs["output_format"].__name__ == "ScoreBatch":
            import re
            ids = re.findall(r'job_id="([^"]+)"', kwargs["messages"][0]["content"])
            parsed = kwargs["output_format"](results=self.scorer(ids))
        else:
            parsed = self.letter()
        return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, usage=usage, model=kwargs["model"])


@pytest.fixture
def settings(tmp_path):
    return Settings(telegram_token="test", telegram_chat_id="42", feed_url="http://feed.test/feed/jobs",
                    db_path=str(tmp_path / "agent.db"), min_score=70, max_digest=5, score_batch=2)


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def agent(settings, store, profile, claude):
    matcher = Matcher(claude, profile, "claude-opus-5-5", "claude-opus-5-5", usage_cb=store.record_usage)
    return Agent(settings, store, FakeTelegram(), matcher, http=None)
