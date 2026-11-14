"""Claude calls: score jobs against the profile, and draft cover letters.

Both use structured output (Pydantic models via `beta.messages.parse`) so the
results are validated before anything is stored or sent. Server-side refusal
fallback is on; a refusal that survives it raises `Refused`.
"""
from typing import Literal

import anthropic
from pydantic import BaseModel, Field

from agent.profile import Profile

BETAS = ["server-side-fallback-2026-07-01"]


class Refused(RuntimeError):
    pass


class JobScore(BaseModel):
    job_id: str
    score: int = Field(ge=0, le=100, description="0-100 fit for this candidate")
    verdict: Literal["strong", "possible", "stretch", "no"]
    reasons: list[str] = Field(description="Up to 3 short reasons this fits, citing the candidate's evidence")
    gaps: list[str] = Field(description="Up to 3 requirements the candidate doesn't clearly meet")
    red_flags: list[str] = Field(description="Hard blockers: citizenship/clearance, far too senior, wrong field, "
                                             "salary well under minimum, not in an acceptable location")


class ScoreBatch(BaseModel):
    results: list[JobScore]


class CoverLetter(BaseModel):
    cover_letter: str = Field(description="Ready-to-send cover letter, 180-260 words, plain text")
    highlights: list[str] = Field(description="2-4 achievements or projects to put first on the CV for this job")
    cv_tweaks: list[str] = Field(description="Up to 3 concrete wording changes to the CV for this job")
    check_before_sending: list[str] = Field(description="Facts the letter assumes that the candidate should confirm")
    cv_summary: str = Field(description="2-3 sentence CV summary tailored to this job, using only profile facts")
    lead_skills: list[str] = Field(description="Up to 12 skills from the profile's Skills list, most relevant first")
    featured_projects: list[str] = Field(description="2-4 project names from the profile, most relevant first")
    why_this_role: str = Field(description="2-3 sentence answer to 'Why do you want this role?' in first person")


SCORING_RULES = """You screen job adverts for one candidate, whose profile is below. Score each job 0-100 for how
likely the candidate is to get an interview AND want the job.

Calibration:
- 85-100 strong: core stack and level match; would shortlist.
- 70-84 possible: most requirements met; one notable gap.
- 50-69 stretch: several gaps or seniority above the candidate's experience.
- 0-49 no: wrong field, far too senior, or a hard blocker in red_flags.

Rules:
- Judge only from the advert text given (often a 500-character snippet); don't invent requirements.
- Count commercial experience honestly from the profile's Experience section; personal projects show skill but
  aren't years of employment. Roles asking for clearly more years than the candidate has are stretch at best.
- Requirements the candidate's stated work rights don't satisfy (e.g. citizenship, security clearance when the
  candidate isn't a citizen) are red flags.
- Return exactly one result per job, using the job_id given."""

LETTER_RULES = """You write a cover letter for the candidate below, for one specific job.
- 180-260 words, plain text, Australian English, warm but direct. No clichés ("I am writing to express").
- Open with why this role, then 2-3 specific pieces of evidence from the profile that match the advert,
  naming real projects and links where relevant. Close with availability and work rights.
- Never claim experience, employers or skills that aren't in the profile. If the advert asks for something
  the candidate lacks, don't mention it; list it in check_before_sending instead.
- Sign off with the candidate's name.

Also tailor the CV. The CV is generated from the profile, and you only choose emphasis:
- cv_summary: 2-3 sentences, first person implied (no "I"), only facts from the profile.
- lead_skills: copy names exactly from the profile's Skills list, most relevant to this advert first.
- featured_projects: copy names exactly from the profile's Projects list, most relevant first.
- why_this_role: a short, specific, first-person answer for an application form."""


class Matcher:
    def __init__(self, client: anthropic.Anthropic, profile: Profile, score_model: str, draft_model: str, usage_cb=None):
        self.client, self.profile = client, profile
        self.score_model, self.draft_model = score_model, draft_model
        self.usage_cb = usage_cb or (lambda purpose, model, usage: None)

    def _system(self, rules: str) -> list[dict]:
        # Rules + profile are identical on every call, so they go first and are cached.
        return [{"type": "text", "text": f"{rules}\n\n<candidate_profile>\n{self.profile.render()}\n</candidate_profile>",
                 "cache_control": {"type": "ephemeral"}}]

    def _parse(self, purpose: str, model: str, rules: str, user: str, output_format):
        response = self.client.beta.messages.parse(
            model=model, max_tokens=16000, betas=BETAS, fallbacks="default",
            output_config={"effort": "low"}, system=self._system(rules),
            messages=[{"role": "user", "content": user}], output_format=output_format)
        self.usage_cb(purpose, response.model, response.usage)
        if response.stop_reason == "refusal":
            raise Refused(f"{purpose} declined")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise RuntimeError(f"{purpose}: incomplete response ({response.stop_reason})")
        return response.parsed_output

    def score(self, jobs: list[dict]) -> list[JobScore]:
        listing = "\n\n".join(_job_block(job) for job in jobs)
        batch = self._parse("score", self.score_model, SCORING_RULES,
                            f"Score these {len(jobs)} jobs:\n\n{listing}", ScoreBatch)
        wanted = {str(job["id"]) for job in jobs}
        # Drop anything not asked for; missing jobs stay unscored and are retried next run.
        return [result for result in batch.results if result.job_id in wanted]

    def letter(self, job: dict, full_advert: str | None = None) -> CoverLetter:
        advert = _job_block(job)
        if full_advert:
            advert += f"\n\nFull advert text pasted by the candidate:\n<advert>\n{full_advert[:12000]}\n</advert>"
        return self._parse("letter", self.draft_model, LETTER_RULES, f"Write the application for:\n\n{advert}",
                           CoverLetter)


def _job_block(job: dict) -> str:
    salary = ""
    if job.get("salary_min") or job.get("salary_max"):
        salary = f"\nSalary: {job.get('salary_min') or '?'} - {job.get('salary_max') or '?'} AUD"
    return (f'<job job_id="{job["id"]}">\nTitle: {job["title"]}\nCompany: {job.get("company") or job.get("company_name") or "unknown"}'
            f'\nLocation: {job.get("location") or "unknown"}{salary}\nPosted: {job.get("posted_at") or "unknown"}'
            f'\nDescription: {job.get("description") or "(none)"}\n</job>')
