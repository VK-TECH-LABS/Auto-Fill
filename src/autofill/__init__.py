"""Job-application form autofill that stops before submit.

Auto-Fill is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option)
any later version. See LICENSE and NOTICE.

Portions of the candidate-profile shape and field-mapping rules are adapted
from ApplyPilot (https://github.com/Pickle-Pixel/ApplyPilot), Copyright
Pickle-Pixel, AGPL-3.0-only.
"""

from autofill.ats import detect_ats
from autofill.credentials import CredentialProvider, Credentials, MemoryCredentialProvider, SiteContext
from autofill.engine import ApplicationResult, AutofillOptions, Status, autofill_application
from autofill.fill import fill_application
from autofill.mapping import map_field
from autofill.models import FillResult, JobContext
from autofill.profile import CandidateProfile, ProfileError
from autofill.safeguards import HUMAN_SUBMIT_ONLY, HumanSubmissionRequired, SubmitBlockedError
from autofill.salary import resolve_salary

__version__ = "0.2.0"

__all__ = [
    "HUMAN_SUBMIT_ONLY",
    "ApplicationResult",
    "AutofillOptions",
    "CandidateProfile",
    "CredentialProvider",
    "Credentials",
    "FillResult",
    "HumanSubmissionRequired",
    "JobContext",
    "MemoryCredentialProvider",
    "ProfileError",
    "SiteContext",
    "Status",
    "SubmitBlockedError",
    "__version__",
    "autofill_application",
    "detect_ats",
    "fill_application",
    "map_field",
    "resolve_salary",
]
