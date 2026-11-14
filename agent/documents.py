"""Tailored CV and cover-letter PDFs, built only from the profile.

Claude decides emphasis (summary wording, skill order, which projects lead),
but every fact on the page comes from profile.yaml: unknown skill or project
names from the model are dropped, so nothing invented reaches an employer.
"""
from datetime import date
from io import BytesIO
import re
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

from agent.matcher import CoverLetter
from agent.profile import Profile

INK, MUTED, ACCENT = HexColor("#1c1d21"), HexColor("#5b5e66"), HexColor("#2357d9")
BASE = ParagraphStyle("base", fontName="Helvetica", fontSize=9.6, leading=13, textColor=INK, alignment=TA_LEFT)
STYLES = {
    "name": ParagraphStyle("name", parent=BASE, fontName="Helvetica-Bold", fontSize=20, leading=24),
    "headline": ParagraphStyle("headline", parent=BASE, fontSize=10.5, leading=14, textColor=ACCENT),
    "contact": ParagraphStyle("contact", parent=BASE, fontSize=8.8, textColor=MUTED),
    "h2": ParagraphStyle("h2", parent=BASE, fontName="Helvetica-Bold", fontSize=10.5, leading=14, spaceBefore=9,
                         spaceAfter=3, textColor=ACCENT),
    "role": ParagraphStyle("role", parent=BASE, fontName="Helvetica-Bold", spaceBefore=4),
    "body": BASE,
    "letter": ParagraphStyle("letter", parent=BASE, fontSize=10.5, leading=15, spaceAfter=8),
}


def _t(text) -> str:
    """Escape for reportlab markup; keep to characters the built-in fonts can draw."""
    text = str(text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = text.encode("cp1252", errors="ignore").decode("cp1252")
    return escape(text)


def _bullets(items: list[str]) -> ListFlowable:
    return ListFlowable([ListItem(Paragraph(_t(item), BASE), leftIndent=10) for item in items],
                        bulletType="bullet", start="•", leftIndent=10, bulletFontSize=8)


def _contact_line(profile: Profile) -> str:
    c = profile.contact
    parts = [c.get("location"), c.get("email"), c.get("phone"),
             (c.get("linkedin") or "").replace("https://www.", ""), (c.get("github") or "").replace("https://", "")]
    return "  |  ".join(_t(p) for p in parts if p)


def _render(story: list, title: str, author: str) -> bytes:
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=17 * mm, rightMargin=17 * mm, topMargin=15 * mm,
                            bottomMargin=15 * mm, title=title, author=author)
    doc.build(story)
    return buffer.getvalue()


def chosen(requested: list[str], available: list[str]) -> list[str]:
    """Keep the model's order but only names that exist in the profile (case-insensitive)."""
    lookup = {name.lower(): name for name in available}
    picked = []
    for name in requested:
        real = lookup.get(name.strip().lower())
        if real and real not in picked:
            picked.append(real)
    return picked


def cv_pdf(profile: Profile, draft: CoverLetter) -> bytes:
    skills = chosen(draft.lead_skills, profile.skills)
    skills += [s for s in profile.skills if s not in skills]
    names = [p.get("name", "") for p in profile.projects]
    order = chosen(draft.featured_projects, names)
    order += [n for n in names if n not in order]
    projects = sorted(profile.projects, key=lambda p: order.index(p.get("name", "")))[:5]

    story = [Paragraph(_t(profile.name), STYLES["name"]), Paragraph(_t(profile.headline), STYLES["headline"]),
             Paragraph(_contact_line(profile), STYLES["contact"]),
             Paragraph("Summary", STYLES["h2"]), Paragraph(_t(draft.cv_summary), BASE),
             Paragraph("Skills", STYLES["h2"]), Paragraph(_t(" · ".join(skills[:20])), BASE),
             Paragraph("Selected projects", STYLES["h2"])]
    for project in projects:
        link = f' <font color="#5b5e66">{_t(project["url"])}</font>' if project.get("url") else ""
        story.append(Paragraph(f'<b>{_t(project.get("name"))}</b>{link}', BASE))
        story.append(Paragraph(_t(project.get("summary")), BASE))
        story.append(Spacer(1, 3))
    story.append(Paragraph("Experience", STYLES["h2"]))
    for job in profile.experience:
        story.append(Paragraph(f'{_t(job.get("title"))}, {_t(job.get("company"))}'
                               f'  <font color="#5b5e66">{_t(job.get("dates"))}</font>', STYLES["role"]))
        if job.get("points"):
            story.append(_bullets(job["points"]))
    story.append(Paragraph("Education", STYLES["h2"]))
    story.append(_bullets(profile.education))
    rights = profile.screening.get("Work rights")
    if rights:
        story += [Paragraph("Work rights", STYLES["h2"]), Paragraph(_t(rights), BASE)]
    return _render(story, f"{profile.name} CV", profile.name)


def letter_pdf(profile: Profile, draft: CoverLetter, job: dict, today: date | None = None) -> bytes:
    today = today or date.today()
    story = [Paragraph(_t(profile.name), STYLES["name"]), Paragraph(_contact_line(profile), STYLES["contact"]),
             Spacer(1, 10), Paragraph(today.strftime("%-d %B %Y"), STYLES["letter"]),
             Paragraph(f'<b>Re: {_t(job["title"])}{" - " + _t(job.get("company")) if job.get("company") else ""}</b>',
                       STYLES["letter"])]
    for paragraph in re.split(r"\n\s*\n", draft.cover_letter.strip()):
        story.append(Paragraph(_t(paragraph).replace("\n", "<br/>"), STYLES["letter"]))
    return _render(story, f"Cover letter - {job['title']}", profile.name)


def file_stem(profile: Profile, job: dict) -> str:
    def slug(text):
        return re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")[:40]
    return f"{slug(profile.name)}_{slug(job.get('company') or job['title'])}"


def screening_text(profile: Profile, draft: CoverLetter) -> list[tuple[str, str]]:
    """(question, answer) pairs to paste into application forms."""
    answers = [("Why this role", draft.why_this_role)]
    answers += [(question, str(answer)) for question, answer in profile.screening.items()]
    links = [profile.contact.get(key) for key in ("linkedin", "github") if profile.contact.get(key)]
    if links:
        answers.append(("Links", "  ".join(links)))
    return answers
