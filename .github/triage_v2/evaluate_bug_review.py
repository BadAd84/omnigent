"""Evaluate the bug review prompt and write comment previews without GitHub access."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from databricks.sdk import WorkspaceClient

from issue_prioritization.areas import AreaCatalog
from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.config import ScoringConfig
from issue_prioritization.event import prioritize_issue, write_event_artifacts
from issue_prioritization.labels import LabelManifest
from issue_prioritization.model_serving import serving_endpoint_classifier
from issue_prioritization.pipeline import PipelineMode

ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-endpoint", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cases", type=Path, default=ROOT / "tests/fixtures/bug_review_cases.json")
    args = parser.parse_args()
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        parser.error("--output-dir must be new or empty so failed runs cannot show stale previews")
    cases = json.loads(args.cases.read_text())
    workspace = WorkspaceClient(profile=args.profile)
    areas = AreaCatalog.from_json(ROOT.parent / "areas.json")
    manifest = LabelManifest.from_json(ROOT.parent / "issue-prioritization-labels.json")
    config = ScoringConfig.default()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    def evaluate(entry):
        index, case = entry
        issue = BronzeIssue(
            index,
            case["title"],
            case["body"],
            "",
            "example-reporter",
            tuple(case["labels"]),
            datetime.now(UTC),
            0,
            0,
        )
        destination = args.output_dir / case["name"]
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "input.json").write_text(json.dumps(case, indent=2) + "\n")
        try:
            run, classification, _, _ = prioritize_issue(
                issue,
                serving_endpoint_classifier(
                    args.model_endpoint, areas, workspace, review_bugs=True
                ),
                config,
                areas,
                manifest,
                f"example-{index}",
                PipelineMode.DRY_RUN,
            )
            write_event_artifacts(
                destination, run, classification, config, args.model_endpoint, "local", issue.labels
            )
            review = classification.bug_review
            actionability = review.actionability.value if review else None
            clarification = review.clarification if review else None
            actual = {
                "type": classification.issue_type.label,
                "actionability": actionability,
                "clarification": clarification is not None,
                "has_steps": bool(clarification and clarification.reproduction_steps),
            }
            passed = (
                actual["type"] == case["expected_type"]
                and (
                    actionability in case["expected_actionability"]
                    if case["expected_actionability"]
                    else actionability is None
                )
                and actual["clarification"] == case["expected_clarification"]
                and ("expected_steps" not in case or actual["has_steps"] == case["expected_steps"])
            )
            result = {"name": case["name"], "passed": passed, "actual": actual}
        except Exception as error:
            result = {"name": case["name"], "passed": False, "error": str(error)}
        print(json.dumps(result), flush=True)
        return result

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(evaluate, enumerate(cases, start=1)))
    (args.output_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    passed = sum(item["passed"] for item in results)
    print(f"{passed}/{len(results)} cases passed; previews: {args.output_dir}")
    raise SystemExit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
