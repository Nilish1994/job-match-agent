"""Cheap rules that decide which jobs are worth a Claude call.

Everything here is deliberately loose: it only drops jobs that are clearly not
software roles or that the candidate said to avoid. Judging fit is Claude's job.
"""
import re

from agent.profile import Profile

ROLE_WORDS = re.compile(
    r"\b(developer|engineer|engineering|software|programmer|full[- ]?stack|front[- ]?end|back[- ]?end|"
    r"web|mobile|react|python|javascript|typescript|node|ai|ml|machine learning|llm|data|"
    r"application|applications|systems? analyst|devops|platform|integration|automation)\b", re.IGNORECASE)


def wanted(job: dict, profile: Profile) -> bool:
    title = job.get("title") or ""
    lowered = title.lower()
    if any(word.lower() in lowered for word in profile.preferences.avoid_title_words):
        return False
    return bool(ROLE_WORDS.search(title)) or bool(set(job.get("skills", [])) & set(profile.skills))


def select(jobs: list[dict], profile: Profile, limit: int) -> list[dict]:
    """Jobs to score, best skill overlap first, capped to keep daily cost bounded."""
    candidates = [job for job in jobs if wanted(job, profile)]
    own = set(profile.skills)
    candidates.sort(key=lambda job: len(set(job.get("skills", [])) & own), reverse=True)
    return candidates[:limit]
