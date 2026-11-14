"""Regenerate site/assets/sample-cv.png from the fictional profile.example.yaml.

Run from the repo root:  pip install -e . pypdfium2 && python site/tools/sample_cv.py
Uses only the example profile ("Alex Example"), never real personal data.
"""
from pathlib import Path

import pypdfium2

from agent.documents import cv_pdf
from agent.matcher import CoverLetter
from agent.profile import Profile

ROOT = Path(__file__).resolve().parents[2]
profile = Profile.load(ROOT / "profile.example.yaml")
draft = CoverLetter(
    cover_letter="", highlights=[], cv_tweaks=[], check_before_sending=[], why_this_role="",
    cv_summary=("Front-end focused full-stack engineer with three years of commercial React experience building "
                "SaaS features, plus recent Python/FastAPI and AI projects. Enjoys practical, user-focused engineering."),
    lead_skills=["React", "TypeScript", "JavaScript", "Next.js", "Python", "FastAPI"],
    featured_projects=["example-dashboard"],
)
pdf = pypdfium2.PdfDocument(cv_pdf(profile, draft))
image = pdf[0].render(scale=2).to_pil()  # A4 at 144 dpi
# Keep the top of the page, where the content is.
width, height = image.size
out = ROOT / "site" / "assets" / "sample-cv.png"
image.crop((0, 0, width, int(height * 0.5))).save(out, optimize=True)
print(out, image.size)
