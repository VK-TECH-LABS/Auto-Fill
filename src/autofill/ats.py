"""Applicant-tracking-system hints.

ApplyPilot does not ship per-ATS DOM scripts. Its apply prompt calls out
multi-page flows for Workday, Taleo, and iCIMS, and a resume-upload step
for Workday and Lever. Those notes are encoded here. Other well-known ATS
hosts are recognized so a caller can label the page; filling them uses the
same generic field mapper.

``ibegin.tcsapps.com`` is the manual-only ATS domain from ApplyPilot's
``sites.yaml`` (flagged there because of a CAPTCHA the agent could not pass).
Auto-Fill does not solve CAPTCHAs either, so that host is left for a person.
SSO hosts listed in that same file are refused rather than logged into.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class AtsInfo:
    """How to treat a recognized application host."""

    name: str
    multipage: bool = False
    resume_first: bool = False
    notes: str = ""


_ATS: tuple[tuple[str, AtsInfo], ...] = (
    (
        "myworkdayjobs.com",
        AtsInfo(
            "workday",
            multipage=True,
            resume_first=True,
            notes="Multi-page. Upload the resume on the pre-fill step, then continue. Do not submit.",
        ),
    ),
    (
        "myworkday.com",
        AtsInfo(
            "workday",
            multipage=True,
            resume_first=True,
            notes="Multi-page. Upload the resume on the pre-fill step, then continue. Do not submit.",
        ),
    ),
    (
        "workday.com",
        AtsInfo(
            "workday",
            multipage=True,
            resume_first=True,
            notes="Multi-page. Upload the resume on the pre-fill step, then continue. Do not submit.",
        ),
    ),
    (
        "taleo.net",
        AtsInfo(
            "taleo",
            multipage=True,
            notes="Multi-page. Fill each page and continue. Do not submit.",
        ),
    ),
    (
        "icims.com",
        AtsInfo(
            "icims",
            multipage=True,
            notes="Multi-page. Fill each page and continue. Do not submit.",
        ),
    ),
    (
        "lever.co",
        AtsInfo(
            "lever",
            resume_first=True,
            notes="Often starts with a resume upload, then the application fields. Do not submit.",
        ),
    ),
    ("greenhouse.io", AtsInfo("greenhouse", notes="Generic field fill. Do not submit.")),
    ("ashbyhq.com", AtsInfo("ashby", notes="Generic field fill. Do not submit.")),
    ("smartrecruiters.com", AtsInfo("smartrecruiters", notes="Generic field fill. Do not submit.")),
    ("jobvite.com", AtsInfo("jobvite", notes="Generic field fill. Do not submit.")),
    ("bamboohr.com", AtsInfo("bamboohr", notes="Generic field fill. Do not submit.")),
    ("workable.com", AtsInfo("workable", notes="Generic field fill. Do not submit.")),
)

# Substring match, matching ApplyPilot's manual_ats check.
MANUAL_ATS_DOMAINS: tuple[str, ...] = ("ibegin.tcsapps.com",)

# Suffix match. Taken from ApplyPilot config/sites.yaml blocked_sso.
BLOCKED_SSO_DOMAINS: tuple[str, ...] = (
    "accounts.google.com",
    "login.microsoftonline.com",
    "okta.com",
    "auth0.com",
    "sso.cisco.com",
)


def _host(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower()


def detect_ats(url: str | None) -> AtsInfo | None:
    """Return ATS metadata when ``url`` is on a known application host."""
    host = _host(url or "")
    if not host:
        return None
    for pattern, info in _ATS:
        if host == pattern or host.endswith("." + pattern):
            return info
    return None


def is_manual_ats(url: str | None) -> bool:
    """True when ApplyPilot would have skipped this host for a person to do by hand."""
    lowered = (url or "").lower()
    return any(domain in lowered for domain in MANUAL_ATS_DOMAINS)


def is_blocked_sso(url: str | None) -> bool:
    """True when the URL is an identity-provider login Auto-Fill will not use."""
    host = _host(url or "")
    if not host:
        return False
    return any(host == domain or host.endswith("." + domain) for domain in BLOCKED_SSO_DOMAINS)
