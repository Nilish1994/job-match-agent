from io import BytesIO

from pypdf import PdfReader

from agent import documents
from agent.matcher import CoverLetter


def draft(**extra):
    return CoverLetter(**{
        "cover_letter": "Dear Hiring Team,\n\nFirst paragraph.\n\nKind regards,\nAlex Example",
        "highlights": [], "cv_tweaks": [], "check_before_sending": [],
        "cv_summary": "Engineer who ships React and FastAPI products.",
        "lead_skills": ["FastAPI", "Quantum Computing", "react"],
        "featured_projects": ["example-dashboard", "Invented Project"],
        "why_this_role": "Because of your product.", **extra})


def text_of(pdf: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(BytesIO(pdf)).pages)


def test_chosen_keeps_order_and_drops_unknown_names():
    assert documents.chosen(["fastapi", "Nope", "React", "FastAPI"], ["React", "FastAPI"]) == ["FastAPI", "React"]


def test_cv_uses_only_profile_facts(profile):
    text = text_of(documents.cv_pdf(profile, draft()))
    assert "Alex Example" in text and "Engineer who ships React and FastAPI products." in text
    assert "Quantum Computing" not in text and "Invented Project" not in text  # model inventions dropped
    skills_line = text.split("\nSkills\n", 1)[1].split("\n", 1)[0]
    assert skills_line.startswith("FastAPI · React")  # tailored order first, the rest follows
    assert skills_line.index("React") < skills_line.index("TypeScript")
    assert "Example Software" in text and "Example University" in text
    assert "alex@example.com" in text and "Full working rights" in text


def test_letter_pdf(profile):
    from datetime import date
    job = {"title": "Python Developer", "company": "Acme & Co"}
    text = text_of(documents.letter_pdf(profile, draft(cover_letter="Dear team,\n\nI build APIs ’quickly’.\n\nAlex"), job,
                                        today=date(2026, 11, 16)))
    assert "16 November 2026" in text and "Re: Python Developer - Acme & Co" in text
    assert "I build APIs 'quickly'." in text


def test_file_stem_and_screening(profile):
    assert documents.file_stem(profile, {"title": "Dev", "company": "Acme Pty. Ltd."}) == "Alex_Example_Acme_Pty_Ltd"
    answers = documents.screening_text(profile, draft())
    assert answers[0] == ("Why this role", "Because of your product.")
    assert ("Notice period", "2 weeks") in answers
    assert answers[-1][0] == "Links"
