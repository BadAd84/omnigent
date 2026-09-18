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
from issue_prioritization.comments import build_triage_comment, preserve_needs_info_deadline
from issue_prioritization.config import ScoringConfig
from issue_prioritization.databricks_io import VolumeArtifactSink, _classification_from_row
from issue_prioritization.domain import InformationStatus, IssueType
from issue_prioritization.event import prioritize_issue, write_event_artifacts
from issue_prioritization.github import GitHubMutationSink
from issue_prioritization.labels import LabelManifest
from issue_prioritization.needs_info import expired_issue
from issue_prioritization.pipeline import PipelineMode

BODY = "Open a session.\nDisconnect Wi-Fi. Reconnect Wi-Fi. The transcript stays blank."
CODE_ONLY_BODY = "This cache race comes from source analysis. Nobody executed the sequence."
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


def _classifier(response, *, enabled=True, closure_confirmed=True):
    def query(prompt):
        if prompt.startswith("Check whether this bug report"):
            return json.dumps({"source_only": closure_confirmed})
        return json.dumps(response)

    return PromptClassifier(query, AreaCatalog({}, {}), review_bugs=enabled)


def _content():
    return IssueContent(7, "Reconnect leaves transcript blank", BODY, ("Bug",), "reporter")


def _issue(body=BODY, labels=("Bug",)):
    return BronzeIssue(7, _content().title, body, "url", "reporter", labels, NOW, 0, 0)


def _event(response, *, issue=None):
    source_only = response.get("bug_review", {}).get("actionability") == "non_actionable"
    issue = issue or _issue(body=CODE_ONLY_BODY if source_only else BODY)
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


@pytest.mark.parametrize("readability", ["not_assessed", "clear"])
def test_incomplete_observed_bug_reuses_needs_info_expiry(readability):
    response = _response(
        evidence_kind="none",
        information_status="needs_info",
        missing_information=["user_impact"],
        bug_review={
            "actionability": "needs_info",
            "reason": "No concrete consequence is described.",
            "readability": readability,
        },
    )
    run, classification, _, _ = _event(response)
    body = build_triage_comment(run.ranked[0], run.mutations[0], ("Bug", "needs-info"), NOW)

    assert classification.information_status == InformationStatus.NEEDS_INFO
    assert "needs-info" in run.mutations[0].labels_add
    assert not run.mutations[0].close_as_non_actionable
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


def test_code_analysis_cannot_be_actionable_without_an_observed_failure():
    response = _response(evidence_kind="code_analysis")
    response["bug_review"]["clarification"]["reproduction_steps"] = []
    with pytest.raises(ValueError, match="code-only evidence"):
        _event(response)


@pytest.mark.parametrize("evidence_kind", ["none", "code_analysis"])
def test_unclear_observation_can_request_details_without_immediate_closure(evidence_kind):
    response = _response(
        evidence_kind=evidence_kind,
        information_status="needs_info",
        missing_information=["observed_behavior"],
        bug_review={
            "actionability": "needs_info",
            "reason": "Did terminal recreation actually fail, or is this inferred from source?",
            "readability": "not_assessed",
        },
    )
    client = ClosureClient()
    client.issue = _issue()
    run, _, planner, states = _event(response)
    plan = run.mutations[0]
    body = build_triage_comment(run.ranked[0], plan, ("Bug",), NOW)

    assert plan.target.needs_info
    assert "needs-info" in plan.labels_add
    assert not plan.close_as_non_actionable
    assert "Did terminal recreation actually fail" in body
    assert "Please update the issue by" in body
    assert "Closing as" not in body
    GitHubMutationSink(client, LabelManifest(()), planner, states).apply_with_plans(
        replace(run, mode=PipelineMode.APPLY)
    )
    assert client.events == ["sync", "labels", "comment"]


def _non_actionable_response():
    return _response(
        evidence_kind="code_analysis",
        information_status="needs_info",
        missing_information=["observed_behavior", "user_impact"],
        bug_review={
            "actionability": "non_actionable",
            "reason": "The report predicts a cache race but describes no observed failure.",
            "readability": "not_assessed",
            "source_only_quote": CODE_ONLY_BODY,
        },
    )


@pytest.mark.parametrize("labels", [("Bug",), ("Bug", "needs-info")])
def test_code_only_bug_previews_immediate_closure_and_comment(tmp_path, labels):
    run, classification, _, _ = _event(
        _non_actionable_response(), issue=_issue(body=CODE_ONLY_BODY, labels=labels)
    )
    write_event_artifacts(
        tmp_path, run, classification, ScoringConfig.default(), "example", "revision", labels
    )
    artifact = json.loads((tmp_path / "event.json").read_text())
    body = (tmp_path / "comment.md").read_text()

    assert artifact["mode"] == "dry_run"
    assert artifact["mutation"]["close_as_non_actionable"] is True
    assert "Closing as **not planned**" in body
    assert "no observed failure" in body
    assert "please open a new issue" in body
    assert "reopen" not in body
    assert "Please update the issue by" not in body
    assert "**Priority:**" not in body
    assert '"needs_info_deadline":null' in body
    assert artifact["mutation"]["target"]["needs_info"] is False
    assert artifact["classification"]["bug_review"]["source_only_quote"] == CODE_ONLY_BODY
    assert "needs-info" not in run.mutations[0].labels_add
    labels_after = (set(labels) - set(run.mutations[0].labels_remove)) | set(
        run.mutations[0].labels_add
    )
    assert "needs-info" not in labels_after

    previous = body.replace('"needs_info_deadline":null', '"needs_info_deadline":"2026-09-25"')
    assert preserve_needs_info_deadline(body, previous) == body

    VolumeArtifactSink(str(tmp_path / "periodic"), ScoringConfig.default()).write(run)
    periodic = json.loads((tmp_path / "periodic/preview/mutations.json").read_text())[0]
    assert periodic["close_as_non_actionable"] is True
    assert "Closing as **not planned**" in periodic["comment"]


@pytest.mark.parametrize("label", ["security", "duplicate", "Pinned"])
def test_existing_exemptions_block_immediate_closure(label):
    run, _, _, _ = _event(
        _non_actionable_response(), issue=_issue(body=CODE_ONLY_BODY, labels=("Bug", label))
    )
    plan = run.mutations[0]
    assert not plan.close_as_non_actionable
    assert f"non_actionable_{label.casefold()}_exempt" in plan.blocked
    assert "Closing as" not in build_triage_comment(run.ranked[0], plan, ("Bug", label), NOW)


def test_observed_evidence_cannot_be_used_to_close_a_bug_as_non_actionable():
    response = _non_actionable_response()
    response["evidence_kind"] = "observed_intermittent"
    with pytest.raises(ValueError, match="cannot claim observed failure evidence"):
        _event(response)


class ClosureClient:
    def __init__(self):
        self.issue = _issue(body=CODE_ONLY_BODY)
        self.events = []
        self.comments = []

    def sync_missing_labels(self, manifest):
        self.events.append("sync")

    def issue_labels(self, issue_number):
        return self.issue.labels

    def issue_for_triage(self, issue_number):
        return self.issue

    def apply_labels(self, issue_number, labels_add, labels_remove):
        self.events.append("labels")
        labels = (set(self.issue.labels) - set(labels_remove)) | set(labels_add)
        self.issue = replace(self.issue, labels=tuple(sorted(labels)))

    def upsert_issue_comment(self, issue_number, body):
        self.events.append("comment")
        self.comments.append(body)
        return 1

    def close_issue(self, issue_number):
        self.events.append("close")


def _closure_apply(client, *, mode=PipelineMode.APPLY):
    run, _, planner, states = _event(_non_actionable_response())
    return GitHubMutationSink(client, LabelManifest(()), planner, states).apply_with_plans(
        replace(run, mode=mode)
    )


def test_apply_posts_explanation_before_closing():
    client = ClosureClient()
    plans = _closure_apply(client)
    assert plans[0].close_as_non_actionable
    assert client.events == ["sync", "labels", "comment", "close"]
    assert "Closing as **not planned**" in client.comments[0]


def test_immediate_closure_removes_the_automatic_reopen_label():
    client = ClosureClient()
    client.issue = replace(client.issue, labels=("Bug", "needs-info"))

    _closure_apply(client)

    assert client.events == ["sync", "labels", "comment", "close"]
    assert "needs-info" not in client.issue.labels
    assert "please open a new issue" in client.comments[0]


def test_observed_failure_is_commented_on_without_closure():
    client = ClosureClient()
    client.issue = _issue()
    run, _, planner, states = _event(_response())
    plans = GitHubMutationSink(client, LabelManifest(()), planner, states).apply_with_plans(
        replace(run, mode=PipelineMode.APPLY)
    )
    assert not plans[0].close_as_non_actionable
    assert client.events == ["sync", "labels", "comment"]
    assert "Problem in plain English" in client.comments[0]


def test_failed_comment_prevents_closure():
    class Client(ClosureClient):
        def upsert_issue_comment(self, issue_number, body):
            raise RuntimeError("comment failed")

    client = Client()
    with pytest.raises(RuntimeError, match="comment failed"):
        _closure_apply(client)
    assert "close" not in client.events


@pytest.mark.parametrize("after_comment", [False, True])
def test_new_evidence_prevents_stale_closure(after_comment):
    class Client(ClosureClient):
        def issue_for_triage(self, issue_number):
            if not after_comment or self.comments:
                return replace(self.issue, body="New observed failure and logs")
            return self.issue

    client = Client()
    with pytest.raises(RuntimeError, match="changed or closed"):
        _closure_apply(client)
    assert "close" not in client.events
    if not after_comment:
        assert "comment" not in client.events
        assert "labels" not in client.events


def test_dry_run_cannot_enter_the_mutation_sink():
    client = ClosureClient()
    with pytest.raises(ValueError, match="require apply mode"):
        _closure_apply(client, mode=PipelineMode.DRY_RUN)
    assert client.events == []


def test_exemption_added_since_classification_prevents_closure():
    client = ClosureClient()
    client.issue = replace(client.issue, labels=("Bug", "security"))
    plans = _closure_apply(client)
    assert not plans[0].close_as_non_actionable
    assert "close" not in client.events
    assert "Closing as" not in client.comments[0]


def test_fabricated_source_quote_aborts_classification():
    response = _response()
    response["bug_review"]["clarification"]["reproduction_steps"][0]["source_quote"] = (
        "Run an undocumented repair command."
    )
    with pytest.raises(ValueError, match="absent from the report"):
        _event(response)


def test_missing_source_only_basis_requests_clarification_instead_of_closure():
    response = _non_actionable_response()
    response["bug_review"].pop("source_only_quote")
    response["missing_information"] = ["version_or_environment"]
    run, classification, _, _ = _event(response)

    assert classification.bug_review.actionability == BugActionability.NEEDS_INFO
    assert "observed_behavior" in classification.missing_information
    assert run.mutations[0].target.needs_info
    assert not run.mutations[0].close_as_non_actionable


def test_fabricated_source_only_basis_cannot_close_a_report():
    response = _non_actionable_response()
    response["bug_review"]["source_only_quote"] = "The author confirms this never happened."
    with pytest.raises(ValueError, match="source_only_quote is absent"):
        _event(response)


@pytest.mark.parametrize("confirmed", [False, None, "true"])
def test_closure_check_must_explicitly_confirm_the_speculative_basis(confirmed):
    issue = _issue(body=CODE_ONLY_BODY)
    run, classification, _, _ = prioritize_issue(
        issue,
        _classifier(_non_actionable_response(), closure_confirmed=confirmed),
        ScoringConfig.default(),
        AreaCatalog({}, {}),
        LabelManifest(()),
        "preview",
        PipelineMode.DRY_RUN,
    )
    assert classification.bug_review.actionability == BugActionability.NEEDS_INFO
    assert classification.bug_review.source_only_quote is None
    assert run.mutations[0].target.needs_info
    assert not run.mutations[0].close_as_non_actionable


def test_source_only_quote_preserves_whitespace_normalization():
    response = _non_actionable_response()
    response["bug_review"]["source_only_quote"] = CODE_ONLY_BODY.replace(". ", ".\n")
    run, classification, _, _ = _event(response)
    assert classification.bug_review.source_only_quote == CODE_ONLY_BODY
    assert run.mutations[0].close_as_non_actionable


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
