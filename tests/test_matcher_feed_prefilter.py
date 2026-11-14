from types import SimpleNamespace

import httpx
import pytest

from agent import feed, prefilter
from agent.matcher import JobScore, Matcher, Refused
from conftest import FakeClaude, make_job


def test_score_request_is_cached_structured_and_has_fallback(profile, claude):
    matcher = Matcher(claude, profile, "claude-opus-5-5", "claude-opus-5-5")
    jobs = [make_job(1), make_job(2)]
    results = matcher.score(jobs)
    assert [r.job_id for r in results] == [jobs[0]["id"], jobs[1]["id"]]
    call = claude.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"] == {"effort": "low"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Alex Example" in call["system"][0]["text"]
    assert "thinking" not in call  # Opus 5.5 always thinks; disabling it would be a 400


def test_score_drops_ids_that_were_not_asked_for(profile):
    stray = lambda ids: [JobScore(job_id="not-asked", score=99, verdict="strong", reasons=[], gaps=[], red_flags=[])]
    matcher = Matcher(FakeClaude(scorer=stray), profile, "m", "m")
    assert matcher.score([make_job(1)]) == []


def test_refusal_raises(profile):
    client = FakeClaude()
    client.beta.messages.parse = lambda **kw: SimpleNamespace(
        stop_reason="refusal", parsed_output=None, model="m",
        usage=SimpleNamespace(input_tokens=1, output_tokens=0, cache_read_input_tokens=0))
    with pytest.raises(Refused):
        Matcher(client, profile, "m", "m").score([make_job(1)])


def test_letter_includes_pasted_advert(profile, claude):
    Matcher(claude, profile, "m", "m").letter(make_job(1), full_advert="We need GraphQL experience.")
    assert "We need GraphQL experience." in claude.calls[0]["messages"][0]["content"]


def test_feed_sync_pages_and_resumes(store):
    jobs = [make_job(n) for n in range(5)]
    seen_params = []

    def handler(request):
        params = dict(request.url.params)
        seen_params.append(params)
        start = 0 if "after_id" not in params else next(i for i, j in enumerate(jobs) if j["id"] == params["after_id"]) + 1
        page = jobs[start:start + int(params["limit"])]
        return httpx.Response(200, json={
            "items": page, "has_more": start + len(page) < len(jobs),
            "next_cursor": {"since": page[-1]["first_seen_at"], "after_id": page[-1]["id"]} if page else None})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert feed.sync(store, "http://feed.test/feed/jobs", client, page_size=2) == 5
        assert feed.sync(store, "http://feed.test/feed/jobs", client, page_size=2) == 0
    assert store.get_meta(feed.CURSOR_KEY)["after_id"] == jobs[-1]["id"]
    assert seen_params[-1]["after_id"] == jobs[-1]["id"]


def test_prefilter(profile):
    keep = make_job(1, title="Junior Python Developer")
    avoid = make_job(2, title="Engineering Manager")
    unrelated = make_job(3, title="Payroll Officer", skills=())
    by_skill = make_job(4, title="Analyst", skills=("React",))
    assert prefilter.wanted(keep, profile)
    assert not prefilter.wanted(avoid, profile)
    assert not prefilter.wanted(unrelated, profile)
    assert prefilter.wanted(by_skill, profile)
    ranked = prefilter.select([make_job(5, skills=("Python",)), make_job(6, skills=("Python", "FastAPI", "Docker"))],
                              profile, limit=1)
    assert ranked[0]["id"].endswith("6")


def test_prefilter_drops_adverts_with_avoided_phrases(profile):
    profile.preferences.avoid_description_phrases = ["Australian citizen", "security clearance"]
    clearance = make_job(7, title="Python Developer", description="Must hold a Baseline Security Clearance.")
    citizen = make_job(8, title="React Developer", description="Applicants must be an Australian Citizen.")
    fine = make_job(9, title="React Developer", description="Visa sponsorship available.")
    assert not prefilter.wanted(clearance, profile)
    assert not prefilter.wanted(citizen, profile)
    assert prefilter.wanted(fine, profile)
