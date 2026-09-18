from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.bug_review import BugActionability, BugReview
from issue_prioritization.classification import IssueContent, PromptClassifier
from issue_prioritization.comments import build_triage_comment
from issue_prioritization.config import ScoringConfig
from issue_prioritization.databricks_io import _classification_from_row
from issue_prioritization.domain import InformationStatus, IssueType
from issue_prioritization.event import prioritize_issue, write_event_artifacts
from issue_prioritization.labels import LabelManifest
from issue_prioritization.needs_info import expired_issue
from issue_prioritization.pipeline import PipelineMode

BODY = "Open a session.\nDisconnect Wi-Fi. Reconnect Wi-Fi. The transcript stays blank."
NOW = datetime(2026, 9, 18, tzinfo=UTC)


def _response(**overrides):
    return {
        "type": "Bug",
        "impact": "medium",
        "area_keys": [],
        "evidence_kind": "direct_steps",
        "information_status": "sufficient",
        "missing_information": [],
        "reasoning": "Reconnecting leaves the user unable to read the session.",
        "bug_review": {
            "actionability": "actionable",
            "reason": "The report describes a specific user action and incorrect result.",
            "readability": "needs_summary",
            "clarification": {
                "summary": "After Wi-Fi reconnects, the session transcript stays blank.",
                "reproduction_steps": [
                    {"text": "Open a session.", "source_quote": "Open a session."},
                    {
                        "text": "Disconnect and reconnect Wi-Fi.",
                        "source_quote": "Disconnect Wi-Fi. Reconnect Wi-Fi.",
                    },
                    {
                        "text": "Observe that the transcript stays blank.",
                        "source_quote": "The transcript stays blank.",
                    },
                ],
            },
        },
        **overrides,
    }


def _classifier(response, *, enabled=True):
    return PromptClassifier(
        lambda _: json.dumps(response), AreaCatalog({}, {}), review_bugs=enabled
    )


def _content():
    return IssueContent(7, "Reconnect leaves transcript blank", BODY, ("Bug",), "reporter")


def _event(response):
    issue = BronzeIssue(7, _content().title, BODY, "url", "reporter", ("Bug",), NOW, 0, 0)
    return prioritize_issue(
        issue,
        _classifier(response),
        ScoringConfig.default(),
        AreaCatalog({}, {}),
        LabelManifest(()),
        "preview",
        PipelineMode.DRY_RUN,
    )


def test_valid_unreadable_bug_produces_a_grounded_comment_preview(tmp_path):
    run, classification, _, _ = _event(_response())
    write_event_artifacts(
        tmp_path, run, classification, ScoringConfig.default(), "example", "revision", ("Bug",)
    )

    artifact = json.loads((tmp_path / "event.json").read_text())
    body = (tmp_path / "comment.md").read_text()
    assert artifact["comment"]["body"] + "\n" == body
    assert artifact["classification"]["bug_review"]["actionability"] == "actionable"
    assert "Problem in plain English" in body
    assert "After Wi-Fi reconnects, the session transcript stays blank." in body
    assert "2. Disconnect and reconnect Wi-Fi." in body
    assert "not been independently verified" in body
    assert "source_quote" not in body
    assert "needs-info" not in artifact["mutation"]["labels_add"]


@pytest.mark.parametrize("actionability", ["needs_info", "non_actionable"])
def test_unsupported_bug_reuses_needs_info_expiry(actionability):
    response = _response(
        evidence_kind="none",
        information_status="needs_info",
        missing_information=["user_impact"],
        bug_review={
            "actionability": actionability,
            "reason": "No concrete consequence is described.",
            "readability": "not_assessed",
        },
    )
    run, classification, _, _ = _event(response)
    body = build_triage_comment(run.ranked[0], run.mutations[0], ("Bug", "needs-info"), NOW)

    assert classification.information_status == InformationStatus.NEEDS_INFO
    assert "needs-info" in run.mutations[0].labels_add
    assert "No concrete consequence is described." in body
    assert "concrete consequence for users" in body
    assert "Problem in plain English" not in body
    issue = {"number": 7, "state": "open", "labels": [{"name": "Bug"}, {"name": "needs-info"}]}
    comments = ({"body": body, "user": {"type": "Bot"}},)
    assert expired_issue(issue, comments, date(2026, 9, 25)) is None
    assert expired_issue(issue, comments, date(2026, 9, 26)).number == 7


@pytest.mark.parametrize("issue_type", ["Feature", "Docs"])
def test_review_never_applies_to_features_or_docs(issue_type):
    result = _classifier(_response(type=issue_type, bug_review="malformed")).classify(_content())
    assert result.issue_type != IssueType.BUG
    assert result.bug_review is None
    assert result.information_status == InformationStatus.NOT_APPLICABLE


def test_disabled_prototype_ignores_review_fields():
    result = _classifier(_response(bug_review="malformed"), enabled=False).classify(_content())
    assert result.bug_review is None


def test_readable_bug_does_not_get_an_extra_summary():
    response = _response()
    response["bug_review"]["clarification"] = None
    response["bug_review"]["readability"] = "clear"
    run, _, _, _ = _event(response)
    body = build_triage_comment(run.ranked[0], run.mutations[0], ("Bug",), NOW)
    assert "Problem in plain English" not in body
    assert "Steps to reproduce" not in body


def test_code_analysis_can_be_actionable_without_inventing_reproduction_steps():
    response = _response(evidence_kind="code_analysis")
    response["bug_review"]["clarification"]["reproduction_steps"] = []
    run, _, _, _ = _event(response)
    body = build_triage_comment(run.ranked[0], run.mutations[0], ("Bug",), NOW)
    assert "Problem in plain English" in body
    assert "No reproduction steps have been inferred" in body
    assert "Steps to reproduce" not in body


def test_fabricated_source_quote_aborts_classification():
    response = _response()
    response["bug_review"]["clarification"]["reproduction_steps"][0]["source_quote"] = (
        "Run an undocumented repair command."
    )
    with pytest.raises(ValueError, match="absent from the report"):
        _event(response)


def test_reproduction_quotes_allow_whitespace_normalization():
    response = _response()
    response["bug_review"]["clarification"]["reproduction_steps"] = [
        {
            "text": "Open a session, then disconnect Wi-Fi.",
            "source_quote": "Open a session.   Disconnect Wi-Fi.",
        }
    ]
    assert _classifier(response).classify(_content()).bug_review is not None


def test_code_fences_in_quoted_evidence_survive_json_parsing():
    source = 'Run this command: ```json\n{"broken": true}\n```'
    response = _response()
    response["bug_review"]["clarification"]["reproduction_steps"] = [
        {"text": "Run the supplied command.", "source_quote": source}
    ]
    classifier = PromptClassifier(
        lambda _: f"```json\n{json.dumps(response)}\n```",
        AreaCatalog({}, {}),
        review_bugs=True,
    )

    result = classifier.classify(replace(_content(), body=source))

    assert result.bug_review.clarification.reproduction_steps[0].source_quote == " ".join(
        source.split()
    )


@pytest.mark.parametrize("review", [None, {}, {"actionability": "valid", "reason": "Reason"}])
def test_opt_in_requires_a_valid_review(review):
    with pytest.raises(ValueError):
        _classifier(_response(bug_review=review)).classify(_content())


def test_inconsistent_actionability_cannot_change_labels():
    response = _response()
    response["bug_review"] = {
        "actionability": "non_actionable",
        "reason": "No actual failure.",
        "readability": "not_assessed",
    }
    with pytest.raises(ValueError, match="disagrees with information status"):
        _event(response)


def test_needs_summary_requires_a_clarification():
    response = _response()
    response["bug_review"]["clarification"] = None
    with pytest.raises(ValueError, match="readability disagrees"):
        _event(response)


def test_clarification_cannot_be_attached_to_an_unsupported_bug():
    response = _response()
    response["bug_review"]["actionability"] = "needs_info"
    with pytest.raises(ValueError, match="only an actionable bug"):
        BugReview.from_mapping(response["bug_review"])


def test_long_summary_or_excessive_steps_are_rejected():
    response = _response()["bug_review"]
    response["clarification"]["summary"] = "x" * 601
    with pytest.raises(ValueError, match="summary"):
        BugReview.from_mapping(response)
    response["clarification"]["summary"] = "Short summary."
    response["clarification"]["reproduction_steps"] *= 3
    with pytest.raises(ValueError, match="at most six"):
        BugReview.from_mapping(response)


def test_generated_comment_cannot_ping_users_or_embed_links():
    response = _response()
    response["bug_review"]["clarification"]["summary"] = (
        "@maintainer <img> ![image](https://example.com)"
    )
    run, _, _, _ = _event(response)
    body = build_triage_comment(run.ranked[0], run.mutations[0], ("Bug",), NOW)
    assert "@maintainer" not in body
    assert "<img>" not in body
    assert "![image](" not in body


def test_cached_review_round_trips_and_nonbugs_drop_it():
    classification = _classifier(_response()).classify(_content())
    row = SimpleNamespace(
        issue_number=7,
        issue_type="Bug",
        impact="medium",
        area_keys=[],
        component_labels=[],
        reasoning="Reason",
        content_hash="hash",
        reported_type="Bug",
        evidence_kind="direct_steps",
        information_status="sufficient",
        missing_information=[],
        bug_review_json=json.dumps(classification.bug_review.as_dict()),
    )
    assert _classification_from_row(row).bug_review == classification.bug_review
    row.issue_type = "Feature"
    assert _classification_from_row(row).bug_review is None


def test_followup_with_evidence_clears_needs_info_and_can_summarize():
    issue = BronzeIssue(
        7, _content().title, BODY, "url", "reporter", ("Bug", "needs-info"), NOW, 0, 0
    )
    run, classification, _, _ = prioritize_issue(
        issue,
        _classifier(_response()),
        ScoringConfig.default(),
        AreaCatalog({}, {}),
        LabelManifest(()),
        "followup",
        PipelineMode.DRY_RUN,
    )
    assert classification.bug_review.actionability == BugActionability.ACTIONABLE
    assert "needs-info" in run.mutations[0].labels_remove
    # Defensive rendering also protects callers loading normalized issue JSON.
    feature = replace(
        run.ranked[0], issue=replace(run.ranked[0].issue, issue_type=IssueType.ENHANCEMENT)
    )
    assert "Problem in plain English" not in build_triage_comment(feature, run.mutations[0], ())
