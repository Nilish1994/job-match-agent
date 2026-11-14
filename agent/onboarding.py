"""Onboarding logic with no I/O: preference options, CV text extraction, and building profiles.

The Telegram conversation itself lives in agent.bot; this module is what it relies on.
"""
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from agent.matcher import ProfileDraft
from agent.profile import Preferences, Profile
from agent.telegram import esc

MAX_CV_BYTES = 5 * 1024 * 1024
MIN_CV_CHARS = 300  # less than this means a scanned/image-only PDF

ROLES = ["Software Engineer", "Full-Stack Developer", "Frontend Developer", "Backend Developer",
         "Mobile Developer", "Data Engineer", "Data Analyst", "AI/ML Engineer", "DevOps / Cloud Engineer",
         "QA / Test Engineer", "Business / Systems Analyst", "IT Support"]
LOCATIONS = {"onsite": "Melbourne (on-site)", "hybrid": "Melbourne (hybrid)", "remote": "Remote within Australia"}
SALARIES = [0, 80_000, 100_000, 120_000, 130_000, 150_000]

CITIZENSHIP_PHRASES = ["australian citizen", "citizens only", "citizenship is required", "must be a citizen",
                       "permanent resident", "security clearance", "baseline clearance", "nv1", "nv2"]
NO_SPONSORSHIP_PHRASES = ["no sponsorship", "unable to sponsor", "not able to sponsor", "unable to offer sponsorship",
                          "unable to provide sponsorship", "sponsorship is not available"]
# key -> (button label, text for the scoring prompt, short answer for application forms, phrases to skip)
WORK_RIGHTS = {
    "citizen": ("🇦🇺 Citizen or permanent resident",
                "Australian citizen or permanent resident: full working rights.",
                "Australian citizen / permanent resident; full working rights.", []),
    "visa": ("🛂 Visa with full work rights",
             "Full working rights in Australia on a visa; no sponsorship needed. Not an Australian citizen or "
             "permanent resident, so adverts requiring citizenship, permanent residency or a security clearance "
             "are red flags.",
             "Full working rights in Australia; no sponsorship required.", CITIZENSHIP_PHRASES),
    "sponsor": ("📝 I need visa sponsorship",
                "Needs employer visa sponsorship to work in Australia. Adverts offering sponsorship are a plus; "
                "adverts requiring citizenship, permanent residency or a clearance, or saying they can't sponsor, "
                "are red flags.",
                "I would require visa sponsorship.", CITIZENSHIP_PHRASES + NO_SPONSORSHIP_PHRASES),
}
AVOID_TITLES = ["Principal", "Head of", "Director", "Manager", "Sales", "Recruiter"]


class CvError(ValueError):
    """A user-facing problem with an uploaded CV."""


def cv_text(data: bytes) -> str:
    if len(data) > MAX_CV_BYTES:
        raise CvError("That file is over 5 MB. Please send a smaller PDF.")
    try:
        reader = PdfReader(BytesIO(data))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:10])
    except (PdfReadError, ValueError, KeyError, TypeError):
        raise CvError("I couldn't read that PDF. Please export your CV to PDF again and resend it.") from None
    if len(text.strip()) < MIN_CV_CHARS:
        raise CvError("That PDF looks like a scan (an image of text), so I can't read it. "
                      "Please send a PDF exported from Word or Google Docs.")
    return text


def profile_from_draft(draft: ProfileDraft, keep: Profile | None = None) -> Profile:
    """A profile from a CV extraction. Re-uploads keep the user's existing preferences and answers."""
    contact = {key: value for key, value in {
        "email": draft.email, "phone": draft.phone, "location": draft.location,
        "linkedin": draft.linkedin, "github": draft.github}.items() if value}
    return Profile(
        name=draft.name.strip() or "Candidate", headline=draft.headline.strip() or "Candidate",
        summary=draft.summary.strip() or draft.headline.strip() or "Candidate",
        experience=[role.model_dump() for role in draft.experience], education=list(draft.education),
        skills=list(dict.fromkeys(draft.skills)),
        projects=[{key: value for key, value in project.model_dump().items() if value} for project in draft.projects],
        preferences=keep.preferences if keep else Preferences(avoid_title_words=list(AVOID_TITLES)),
        contact=contact, screening=dict(keep.screening) if keep else {})


def apply_preferences(profile: Profile, choices: dict) -> Profile:
    """Turn the onboarding answers into scoring preferences and paste-ready screening answers."""
    rights_key = choices.get("rights", "citizen")
    _, rights_text, rights_answer, avoid = WORK_RIGHTS[rights_key]
    locations = [LOCATIONS[key] for key in choices.get("locations", []) if key in LOCATIONS] or list(LOCATIONS.values())
    salary = int(choices.get("salary") or 0) or None
    profile.preferences = Preferences(
        roles=list(choices.get("roles", [])), locations=locations, min_salary=salary, work_rights=rights_text,
        notice=profile.preferences.notice, avoid_title_words=profile.preferences.avoid_title_words or list(AVOID_TITLES),
        avoid_description_phrases=list(avoid),
        notes=("Most adverts don't show a salary; don't penalise that, but treat an advertised maximum well below "
               f"${salary:,} as a significant gap." if salary else ""))
    screening = {"Work rights": rights_answer}
    if salary:
        screening["Salary expectation"] = f"From ${salary:,} + super, depending on the role"
    screening["Location"] = "; ".join(locations)
    # Keep any answers the user already had that we don't generate (e.g. notice period).
    profile.screening = {**{k: v for k, v in profile.screening.items() if k not in screening}, **screening}
    return profile


def summary(profile: Profile) -> str:
    p = profile.preferences
    lines = [f"<b>{esc(profile.name)}</b>", esc(profile.headline), ""]
    if profile.experience:
        lines.append("<b>Experience</b>")
        lines += [f"• {esc(r.get('title'))}, {esc(r.get('company'))} ({esc(r.get('dates'))})" for r in profile.experience[:5]]
    if profile.education:
        lines += ["", "<b>Education</b>"] + [f"• {esc(e)}" for e in profile.education[:3]]
    if profile.skills:
        lines += ["", f"<b>Skills</b>: {esc(', '.join(profile.skills[:20]))}"]
    if profile.projects:
        lines += ["", "<b>Projects</b>: " + esc(", ".join(p_.get("name", "") for p_ in profile.projects[:6]))]
    contact = [profile.contact.get(k) for k in ("email", "phone", "location") if profile.contact.get(k)]
    lines += ["", f"<b>Contact on your CV</b>: {esc(' · '.join(contact)) if contact else 'none found'}"]
    if p.roles:
        lines += ["", f"<b>Looking for</b>: {esc(', '.join(p.roles))}",
                  f"<b>Where</b>: {esc('; '.join(p.locations))}",
                  f"<b>Salary floor</b>: {f'${p.min_salary:,}' if p.min_salary else 'not set'}",
                  f"<b>Work rights</b>: {esc(profile.screening.get('Work rights', ''))}"]
    return "\n".join(lines)
