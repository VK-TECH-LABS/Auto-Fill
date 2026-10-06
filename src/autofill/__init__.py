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
from autofill.fill import fill_application
from autofill.mapping import map_field
from autofill.models import FillResult, JobContext
from autofill.profile import CandidateProfile, ProfileError
from autofill.safeguards import HUMAN_SUBMIT_ONLY, SubmitBlockedError
from autofill.salary import resolve_salary

__version__ = "0.1.0"

__all__ = [
    "HUMAN_SUBMIT_ONLY",
    "CandidateProfile",
    "FillResult",
    "JobContext",
    "ProfileError",
    "SubmitBlockedError",
    "__version__",
    "detect_ats",
    "fill_application",
    "map_field",
    "resolve_salary",
]
