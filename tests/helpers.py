"""Objects shared by service tests.

Pytest adds the repository root via ``pythonpath`` in ``pyproject.toml``, so
this module imports as ``tests.helpers`` without a ``tests`` package init and
without importing another test module.
"""

from __future__ import annotations


def job_context():
    """Application URL used by the HTTP service tests. No candidate data."""
    from autofill.models import JobContext

    return JobContext(url="https://boards.greenhouse.io/example/jobs/1")


def empty_credentials():
    """Credential provider with nothing stored."""
    from autofill.credentials import MemoryCredentialProvider

    return MemoryCredentialProvider()
