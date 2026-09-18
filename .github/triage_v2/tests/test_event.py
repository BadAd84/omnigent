from __future__ import annotations

import json
import sys
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from issue_prioritization import event
from issue_prioritization.areas import Area, AreaCatalog
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.bug_review import BugActionability, BugReview
from issue_prioritization.classification import Classification
from issue_prioritization.config import ScoringConfig
from issue_prioritization.domain import (
    EvidenceKind,
    Impact,
    InformationStatus,
    IssueType,
    MissingInformation,
)
from issue_prioritization.event import (
    _apply_intake,
    prioritize_issue,
    target_for_labels,
    write_event_artifacts,
    write_event_status,
)
from issue_prioritization.github import GitHubClient
from issue_prioritization.intake import IntakePlan
from issue_prioritization.labels import LabelDefinition, LabelManifest
from issue_prioritization.pipeline import PipelineMode
from issue_prioritization.scoring import ScoreEngine


class FakeClassifier:
    def classify(self, issue):
        return Classification(
            issue_number=issue.number,
            issue_type=IssueType.BUG,
            impact=Impact.HIGH,
            area_keys=("db",),
            component_labels=("comp:db",),
            reasoning="Breaks session startup.",
            content_hash=issue.content_hash,
        )


def _issue(labels=()) -> BronzeIssue:
    return BronzeIssue(
        number=7,
        title="Session fails",
        body="Cannot start a session",
        url="https://github.com/omnigent-ai/omnigent/issues/7",
        author="community",
        labels=labels,
        created_at=datetime(2026, 8, 6, tzinfo=UTC),
        upvote_count=0,
        duplicate_count=0,
    )


def _areas() -> AreaCatalog:
    area = Area("db", "comp:db", Decimal("1.2"))
    return AreaCatalog({"db": area}, {"comp:db": (area,)})


def _manifest() -> LabelManifest:
    return LabelManifest((LabelDefinition("comp:db", "000000", ""),))


def _cli_args(tmp_path):
    areas = tmp_path / "areas.json"
    areas.write_text(json.dumps({"areas": [{"key": "db", "label": "comp:db", "weight": 1.2}]}))
    manifest = tmp_path / "labels.json"
    manifest.write_text(
        json.dumps({"labels": [{"name": "comp:db", "color": "000000", "description": ""}]})
    )
    return [
        "issue-priority-event",
        "--issue-number",
        "7",
        "--github-repo",
        "org/repo",
        "--model-endpoint",
        "test-endpoint",
        "--areas",
        str(areas),
        "--label-manifest",
        str(manifest),
        "--output-dir",
        str(tmp_path / "output"),
        "--run-id",
        "preview-closed",
    ]


@pytest.mark.parametrize("include_closed", [False, True])
def test_event_closed_issue_preview_only_reads_github(tmp_path, monkeypatch, include_closed):
    payload = {
        "number": 7,
        "title": "Session fails",
        "body": "Original report",
        "user": {"login": "community"},
        "created_at": "2026-08-06T00:00:00Z",
        "state": "closed",
    }
    calls = []

    def transport(method, path, body):
        calls.append((method, path))
        assert method == "GET"
        if path == "/issues/7":
            return payload
        assert path == "/issues/7/comments?per_page=100&page=1"
        return [{"user": {"login": "community"}, "body": "Additional evidence"}]

    class PreviewClassifier(FakeClassifier):
        def classify(self, issue):
            assert "Additional evidence" in issue.body
            return super().classify(issue)

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        event, "GitHubClient", lambda token, repo: GitHubClient(token, repo, transport)
    )
    monkeypatch.setattr(event, "serving_endpoint_classifier", lambda *a, **kw: PreviewClassifier())
    args = _cli_args(tmp_path)
    if include_closed:
        args.append("--include-closed")
    monkeypatch.setattr(sys, "argv", args)

    event.main()

    output = tmp_path / "output"
    result = json.loads((output / "event.json").read_text())
    assert result["status"] == ("planned" if include_closed else "skipped")
    if include_closed:
        assert result["mode"] == "dry_run"
        assert result["classification"]["type"] == "Bug"
        assert (output / "comment.md").is_file()
        assert len(calls) == 2
    else:
        assert result["reason"] == "issue_not_open"
        assert len(calls) == 1


def test_event_rejects_closed_issue_apply_before_accessing_github(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(sys, "argv", _cli_args(tmp_path) + ["--include-closed", "--mode", "apply"])

    with pytest.raises(SystemExit) as error:
        event.main()

    assert error.value.code == 2
    assert "--include-closed requires --mode dry_run" in capsys.readouterr().err
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("after_comment", [False, True])
def test_event_records_skipped_stale_closure(tmp_path, monkeypatch, after_comment):
    payload = {
        "number": 7,
        "title": "Possible session failure",
        "body": "Source inspection only; never executed.",
        "user": {"login": "community"},
        "labels": [{"name": "Bug"}],
        "created_at": "2026-08-06T00:00:00Z",
        "state": "open",
    }
    writes = []

    def transport(method, path, body):
        if method == "GET":
            if path.startswith("/labels?"):
                return [{"name": "comp:db"}]
            return [] if "/comments" in path else payload
        writes.append((method, path, body))
        assert path != "/issues/7", "Stale evidence must not close the issue"
        if path.endswith("/comments"):
            payload["body"] += "\nI reproduced this in a running session today."
            return {"id": 1}
        assert path.endswith("/labels")
        payload["labels"] += [{"name": label} for label in body["labels"]]
        return {}

    class Classifier(FakeClassifier):
        def classify(self, issue):
            classification = replace(
                super().classify(issue),
                evidence_kind=EvidenceKind.CODE_ANALYSIS,
                information_status=InformationStatus.NEEDS_INFO,
                missing_information=(MissingInformation.OBSERVED_BEHAVIOR,),
                bug_review=BugReview(
                    BugActionability.NON_ACTIONABLE,
                    "No observed failure in the reviewed report.",
                    source_only_quote=issue.body,
                ),
            )
            if not after_comment:
                payload["body"] += "\nI reproduced this in a running session today."
            return classification

    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        event, "GitHubClient", lambda token, repo: GitHubClient(token, repo, transport)
    )
    monkeypatch.setattr(event, "serving_endpoint_classifier", lambda *a, **kw: Classifier())
    monkeypatch.setattr(sys, "argv", _cli_args(tmp_path) + ["--review-bugs", "--mode", "apply"])

    event.main()

    result = json.loads((tmp_path / "output/event.json").read_text())
    assert result["status"] == "skipped_stale"
    assert not result["mutation"]["close_as_non_actionable"]
    assert "non_actionable_stale_assessment" in result["mutation"]["blocked"]
    assert "Automatic closure skipped" in result["comment"]["body"]
    if after_comment:
        assert result["applied_bot_state"]["priority"] == "P1-high"
        assert "recommend closing" in writes[-1][2]["body"]
    else:
        assert writes == []
        assert result["applied_bot_state"] is None


def test_event_grades_and_plans_labels_for_one_issue() -> None:
    run, classification, _, _ = prioritize_issue(
        _issue(),
        FakeClassifier(),
        ScoringConfig.default(),
        _areas(),
        _manifest(),
        "github-1",
        PipelineMode.APPLY,
    )

    assert classification.impact == Impact.HIGH
    assert run.ranked[0].result.score == Decimal("72.00")
    assert set(run.mutations[0].labels_add) == {
        "Bug",
        "P1-high",
        "comp:db",
    }


def test_event_preserves_human_priority_and_retires_severity_label() -> None:
    run, _, _, _ = prioritize_issue(
        _issue(("P3-low", "severity:S3")),
        FakeClassifier(),
        ScoringConfig.default(),
        _areas(),
        _manifest(),
        "github-2",
        PipelineMode.APPLY,
    )

    assert run.ranked[0].issue.impact == Impact.HIGH
    assert run.ranked[0].result.priority.value == "P1-high"
    assert run.mutations[0].labels_add == ("Bug", "comp:db")
    assert run.mutations[0].labels_remove == ("severity:S3",)
    assert run.mutations[0].blocked == ("priority_human_override",)


def test_event_artifact_contains_classification_and_mutation(tmp_path) -> None:
    issue = _issue()
    config = ScoringConfig.default()
    run, classification, _, _ = prioritize_issue(
        issue,
        FakeClassifier(),
        config,
        _areas(),
        _manifest(),
        "github-3",
        PipelineMode.DRY_RUN,
    )

    write_event_artifacts(
        tmp_path,
        run,
        classification,
        config,
        "test-endpoint",
        "abc123",
        issue.labels,
    )

    payload = json.loads((tmp_path / "event.json").read_text())
    assert payload["status"] == "planned"
    assert payload["classification"]["type"] == "Bug"
    assert payload["schema_version"] == 2
    assert payload["classification"]["impact"] == "high"
    assert payload["classification"]["reasoning"] == "Breaks session startup."
    assert payload["classification"]["evidence_kind"] == "none"
    assert payload["classification"]["information_status"] == "not_applicable"
    assert payload["classification"]["missing_information"] == []
    assert payload["score"]["score"] == 72.0
    assert payload["mutation"]["target"]["priority"] == "P1-high"
    assert payload["mutation"]["target"]["issue_type"] == "Bug"
    assert payload["mutation"]["target"]["needs_info"] is False
    assert payload["model_endpoint"] == "test-endpoint"
    assert payload["source_revision"] == "abc123"
    assert "<!-- omnigent-issue-prioritization-v2" in payload["comment"]["body"]
    assert '"base_score":60.0' in payload["comment"]["body"]
    assert {path.name for path in tmp_path.iterdir()} == {
        "comment.md",
        "config.json",
        "event.json",
        "mutations.json",
    }

    write_event_status(
        tmp_path,
        run,
        classification,
        "test-endpoint",
        "abc123",
        issue.labels,
        status="apply_unknown",
    )
    assert json.loads((tmp_path / "event.json").read_text())["status"] == "apply_unknown"


def test_event_ignores_a_retired_severity_label_when_recomputing() -> None:
    issue = _issue()
    config = ScoringConfig.default()
    areas = _areas()
    run, classification, _, _ = prioritize_issue(
        issue,
        FakeClassifier(),
        config,
        areas,
        _manifest(),
        "github-4",
        PipelineMode.APPLY,
    )

    target = target_for_labels(
        issue,
        classification,
        run.scored_at,
        ("severity:S3",),
        ScoreEngine(config, areas),
    )

    assert target.priority == "P1-high"


def test_intake_assigns_before_duplicate_closure() -> None:
    events = []

    class Client:
        def apply_labels(self, issue_number, labels_add, labels_remove):
            events.append("labels")

        def comment_on_issue_once(self, issue_number, marker, body):
            events.append("comment")

        def issue_data(self, issue_number):
            return {"state": "open", "assignees": []}

        def assign_issue(self, issue_number, assignee):
            events.append("assign")

        def close_as_duplicate(self, issue_number, duplicate_of):
            events.append("close")

    plan = IntakePlan(
        ("triaged", "duplicate"),
        ("needs-triage",),
        "owner",
        "duplicate",
        3,
        (),
        0.99,
        "<!-- omnigent-duplicate-check -->\nClosing",
        True,
    )

    _apply_intake(Client(), 7, plan)

    assert events == ["labels", "comment", "assign", "close"]
