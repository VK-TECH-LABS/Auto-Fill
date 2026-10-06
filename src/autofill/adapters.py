"""ATS adapters.

Each adapter names the steps that site usually shows. Filling still uses the
shared field mapper. Adapters do not click Submit; the central guard is the
only click path, and it rejects final application submission.
"""

from __future__ import annotations

from dataclasses import dataclass

from autofill.mapping import normalize


@dataclass(frozen=True)
class AtsAdapter:
    name: str
    steps: tuple[str, ...]
    notes: str


ADAPTERS: dict[str, AtsAdapter] = {
    "workday": AtsAdapter(
        "workday",
        (
            "sign in",
            "my information",
            "contact",
            "address",
            "experience",
            "education",
            "websites",
            "questions",
            "voluntary",
            "review",
        ),
        "Workday: sign in, then My Information through Review. Next and Save and Continue only.",
    ),
    "greenhouse": AtsAdapter(
        "greenhouse",
        ("basics", "links", "employment", "education", "questions", "resume", "review"),
        "Greenhouse: basics, links, employment, education, and explicit questions.",
    ),
    "lever": AtsAdapter(
        "lever",
        ("links", "employment", "education", "resume", "questions", "review"),
        "Lever: links, employment, education, questions. Resume stays with the person.",
    ),
    "ashby": AtsAdapter(
        "ashby",
        ("basics", "links", "employment", "education", "questions", "resume", "review"),
        "Ashby: same shared matcher as Greenhouse.",
    ),
    "smartrecruiters": AtsAdapter(
        "smartrecruiters",
        ("basics", "experience", "education", "questions", "resume", "review"),
        "SmartRecruiters: basics, experience, education, and explicit questions.",
    ),
    "oracle": AtsAdapter(
        "oracle",
        ("login", "basics", "contact", "address", "experience", "education", "questions", "review"),
        "Oracle Candidate Experience: login, then multi-step Next/Continue/Review. Do not submit.",
    ),
    "icims": AtsAdapter(
        "icims",
        ("login", "contact", "experience", "education", "questions", "review"),
        "iCIMS: shared matcher, multi-page Next. Do not submit.",
    ),
    "taleo": AtsAdapter(
        "taleo",
        ("login", "personal", "experience", "education", "questions", "review"),
        "Taleo: shared matcher, multi-page Next. Do not submit.",
    ),
    "successfactors": AtsAdapter(
        "successfactors",
        ("login", "profile", "experience", "education", "questions", "review"),
        "SuccessFactors: shared matcher. Do not submit.",
    ),
    "dayforce": AtsAdapter(
        "dayforce",
        ("login", "personal", "experience", "education", "questions", "review"),
        "Dayforce: shared matcher. Do not submit.",
    ),
}


def adapter_for(name: str | None) -> AtsAdapter | None:
    if not name:
        return None
    return ADAPTERS.get(name)


def page_step(ats_name: str | None, heading: str, url: str = "") -> str:
    """Best-effort step label from the heading and URL. Unknown pages stay generic."""
    blob = normalize(f"{heading} {url}")
    adapter = adapter_for(ats_name)
    if adapter:
        for step in adapter.steps:
            if step in blob:
                return step
    if "review" in blob:
        return "review"
    if "resume" in blob or "cv" in blob:
        return "resume"
    if "sign in" in blob or "log in" in blob:
        return "login"
    return "application"
