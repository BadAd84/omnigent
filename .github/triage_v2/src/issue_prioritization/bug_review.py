from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum

BUG_REVIEW_VERSION = 2


class BugActionability(StrEnum):
    ACTIONABLE = "actionable"
    NEEDS_INFO = "needs_info"
    NON_ACTIONABLE = "non_actionable"


@dataclass(frozen=True)
class ReproductionStep:
    text: str
    source_quote: str


@dataclass(frozen=True)
class BugClarification:
    summary: str
    reproduction_steps: tuple[ReproductionStep, ...] = ()


@dataclass(frozen=True)
class BugReview:
    actionability: BugActionability
    reason: str
    clarification: BugClarification | None = None
    rubric_version: int = BUG_REVIEW_VERSION

    @classmethod
    def from_mapping(cls, value: object) -> BugReview:
        if not isinstance(value, Mapping):
            raise ValueError("bug_review must be an object")
        actionability = BugActionability(value.get("actionability"))
        reason = _text(value.get("reason"), "reason", 500)
        clarification = None
        raw = value.get("clarification")
        if raw is not None:
            if actionability != BugActionability.ACTIONABLE or not isinstance(raw, Mapping):
                raise ValueError("only an actionable bug can have a clarification object")
            steps = raw.get("reproduction_steps", [])
            if not isinstance(steps, (list, tuple)) or len(steps) > 6:
                raise ValueError("reproduction_steps must contain at most six steps")
            parsed_steps = []
            for step in steps:
                if not isinstance(step, Mapping):
                    raise ValueError("each reproduction step must be an object")
                parsed_steps.append(
                    ReproductionStep(
                        _text(step.get("text"), "step text", 300),
                        _text(step.get("source_quote"), "source_quote", 2000),
                    )
                )
            clarification = BugClarification(
                _text(raw.get("summary"), "summary", 600), tuple(parsed_steps)
            )
        review = cls(actionability, reason, clarification, int(value.get("rubric_version", 1)))
        if value.get("readability") != review.readability:
            raise ValueError("bug readability disagrees with actionability or clarification")
        return review

    @property
    def readability(self) -> str:
        if self.actionability != BugActionability.ACTIONABLE:
            return "not_assessed"
        return "needs_summary" if self.clarification is not None else "clear"

    def validate_source(self, body: str) -> None:
        if self.clarification is None:
            return
        source = " ".join(body.split())
        for step in self.clarification.reproduction_steps:
            if step.source_quote not in source:
                raise ValueError("reproduction step source_quote is absent from the report")

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "readability": self.readability}


def _text(value: object, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"bug review {field} must be text")
    text = " ".join(value.split())
    if not text or len(text) > limit:
        raise ValueError(f"bug review {field} must contain 1–{limit} characters")
    return text
