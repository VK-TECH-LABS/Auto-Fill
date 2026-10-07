"""Authenticated HTTP service for the public Auto-Fill program.

Callers are separate applications. This package does not import them and
does not open their databases. See docs/HTTP_SERVICE.md, LICENSE, and NOTICE.
"""

from autofill.service.app import create_app

__all__ = ["create_app"]
