"""Shared data objects for field descriptions and fill results."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Option:
    """One choice in a select or radio group."""

    value: str
    label: str
    selector: str = ""


@dataclass
class Control:
    """A single form control discovered on the page."""

    kind: str
    name: str = ""
    element_id: str = ""
    label: str = ""
    placeholder: str = ""
    aria_label: str = ""
    autocomplete: str = ""
    required: bool = False
    hidden: bool = False
    disabled: bool = False
    read_only: bool = False
    options: list[Option] = field(default_factory=list)
    selector: str = ""
    input_mode: str = ""
    input_type: str = ""
    role: str = ""
    nearby: str = ""
    group: str = ""


@dataclass
class ButtonControl:
    """A button or submit input. Auto-Fill never activates submit controls."""

    name: str
    selector: str = ""
    control_type: str = ""


@dataclass
class PageSnapshot:
    """Fields and buttons extracted from one application page."""

    controls: list[Control]
    buttons: list[ButtonControl]
    captcha_present: bool = False
    password_present: bool = False
    heading: str = ""
    banner: str = ""
    alerts: list[str] = field(default_factory=list)


@dataclass
class MappedField:
    """What the mapper decided to do with one control."""

    key: str | None = None
    action: str = "unanswered"
    text: str = ""
    option_label: str = ""
    option_value: str = ""
    option_selector: str = ""
    reason: str = ""
    field_class: str = ""
    confidence: str = "high"


@dataclass
class JobContext:
    """Optional posting details used only to format compensation answers.

    Auto-Fill does not search for jobs. Callers that already know a posted
    salary range can pass it so the answer stays inside the profile floor.
    """

    title: str = ""
    company: str = ""
    url: str = ""
    posted_salary_min: int | None = None
    posted_salary_max: int | None = None


@dataclass
class FieldOutcome:
    """What happened to one control during a fill."""

    selector: str
    label: str
    key: str | None
    action: str
    detail: str = ""
    field_class: str = ""


@dataclass
class FillResult:
    """Outcome of a fill run.

    ``stopped_before_submit`` is always true. This package has no mode that
    clicks the final Submit or Apply control.
    """

    status: str
    stopped_before_submit: bool = True
    ats: str | None = None
    captcha_present: bool = False
    login_wall: bool = False
    pages_filled: int = 0
    fields: list[FieldOutcome] = field(default_factory=list)
    submit_controls: list[str] = field(default_factory=list)
    continued_controls: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    resume_required: bool = False
    manual_actions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.stopped_before_submit:
            raise ValueError(
                "stopped_before_submit cannot be false. "
                "Auto-Fill never clicks the final Submit or Apply control."
            )

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "stopped_before_submit": self.stopped_before_submit,
            "ats": self.ats,
            "captcha_present": self.captcha_present,
            "login_wall": self.login_wall,
            "pages_filled": self.pages_filled,
            "fields": [
                {
                    "selector": item.selector,
                    "label": item.label,
                    "key": item.key,
                    "action": item.action,
                    "detail": item.detail,
                    "field_class": item.field_class,
                }
                for item in self.fields
            ],
            "submit_controls": list(self.submit_controls),
            "continued_controls": list(self.continued_controls),
            "messages": list(self.messages),
            "resume_required": self.resume_required,
            "manual_actions": list(self.manual_actions),
        }
