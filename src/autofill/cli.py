"""Command line wrapper. Prints the fill result and always stops before submit."""

from __future__ import annotations

import argparse
import json
import os
import sys

from autofill import __version__
from autofill.fill import fill_application
from autofill.profile import CandidateProfile, ProfileError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autofill",
        description=(
            "Fill a job application form from a candidate profile and stop. "
            "This command never clicks Submit or Apply."
        ),
    )
    parser.add_argument("--version", action="version", version=f"autofill {__version__}")
    parser.add_argument("--url", required=True, help="Application form URL, not a job-search page.")
    parser.add_argument(
        "--profile",
        help="Path to candidate profile JSON. Defaults to AUTOFILL_PROFILE.",
    )
    parser.add_argument(
        "--resume",
        help="Accepted for compatibility and ignored. A person uploads the resume.",
    )
    parser.add_argument("--cover-letter", help="Cover letter file to upload when the form asks for one.")
    parser.add_argument(
        "--advance-pages",
        action="store_true",
        help="Click Next, Continue, or Review on multi-page forms. Submit stays blocked.",
    )
    parser.add_argument("--max-pages", type=int, default=5, help="Page cap used with --advance-pages.")
    parser.add_argument("--headed", action="store_true", help="Show the browser window.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    profile_path = args.profile or os.environ.get("AUTOFILL_PROFILE")
    if not profile_path:
        parser.error("Pass --profile or set AUTOFILL_PROFILE. Do not commit that file.")
    resume = args.resume or os.environ.get("AUTOFILL_RESUME") or None
    try:
        profile = CandidateProfile.load(profile_path)
    except ProfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    result = fill_application(
        profile,
        url=args.url,
        resume_path=resume,
        cover_letter_path=args.cover_letter,
        advance_pages=args.advance_pages,
        max_pages=args.max_pages,
        headless=not args.headed,
    )
    print(json.dumps(result.to_dict(), indent=2))
    print(result.messages[0], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
