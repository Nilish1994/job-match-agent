"""The candidate profile the agent matches jobs against (YAML, kept out of git)."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Preferences:
    roles: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    min_salary: int | None = None
    work_rights: str = ""
    notice: str = ""
    avoid_title_words: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Profile:
    name: str
    headline: str
    summary: str
    experience: list[dict]
    education: list[str]
    skills: list[str]
    projects: list[dict]
    preferences: Preferences
    contact: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        missing = [key for key in ("name", "headline", "summary") if not data.get(key)]
        if missing:
            raise ValueError(f"profile is missing: {', '.join(missing)}")
        return cls(
            name=data["name"], headline=data["headline"], summary=data["summary"].strip(),
            experience=data.get("experience", []), education=data.get("education", []),
            skills=data.get("skills", []), projects=data.get("projects", []),
            preferences=Preferences(**(data.get("preferences") or {})),
            contact=data.get("contact") or {},
        )

    def render(self) -> str:
        """Plain-text profile for prompts. Stable output keeps the prompt cache warm."""
        lines = [f"# {self.name}", self.headline, "", "## Summary", self.summary, "", "## Experience"]
        for job in self.experience:
            lines.append(f"- {job.get('title')} at {job.get('company')} ({job.get('dates', '')})")
            lines += [f"  - {point}" for point in job.get("points", [])]
        lines += ["", "## Education"] + [f"- {item}" for item in self.education]
        lines += ["", "## Skills", ", ".join(self.skills), "", "## Projects"]
        for project in self.projects:
            lines.append(f"- {project.get('name')} ({project.get('url', '')}): {project.get('summary', '')}")
        p = self.preferences
        lines += ["", "## Preferences",
                  f"- Target roles: {', '.join(p.roles)}",
                  f"- Locations: {', '.join(p.locations)}",
                  f"- Minimum salary: {f'${p.min_salary:,} AUD' if p.min_salary else 'not set'}",
                  f"- Work rights: {p.work_rights}",
                  f"- Notice period: {p.notice}"]
        if p.notes:
            lines.append(f"- Notes: {p.notes}")
        return "\n".join(lines)
