"""Exercise Polly's workflow cleanup and GitHub output handoff in a real shell."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.posix_only

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/polly-review.yml"
_MARKER = "<!-- POLLY_REVIEW_START -->"
_REVIEW = "## Blocking issues\nNone.\n## Summary\nDone.\n"
_QUOTED_REVIEW = f"## Blocking issues\nDo not strip inline `{_MARKER}` text.\n"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(f"{_MARKER}\n{_REVIEW}", _REVIEW, id="single-review"),
        pytest.param(
            f"{_MARKER}\n## Blocking issues\nPartial draft\n{_MARKER}\n{_REVIEW}",
            _REVIEW,
            id="draft-then-final",
        ),
        pytest.param(f"{_MARKER}\n{_QUOTED_REVIEW}", _QUOTED_REVIEW, id="inline-quote"),
        pytest.param(
            f"{_MARKER}\nPartial draft\n{_MARKER}\n{_QUOTED_REVIEW}",
            _QUOTED_REVIEW,
            id="draft-then-inline-quote",
        ),
        pytest.param(f"Starting review.\n{_REVIEW}", _REVIEW, id="heading-fallback"),
        pytest.param(_QUOTED_REVIEW, _QUOTED_REVIEW, id="heading-with-inline-quote"),
        pytest.param(f"Will emit `{_MARKER}` later.\n", "", id="narration-with-inline-quote"),
        pytest.param("Waiting for results.\n", "", id="narration-only"),
        pytest.param("", "", id="empty"),
        pytest.param(" \n\t", "", id="whitespace-only"),
        pytest.param(f"{_MARKER}\n{_REVIEW}{_MARKER}\n", "", id="empty-final-review"),
        pytest.param(f"{_MARKER}\n \t\n", "", id="whitespace-final-review"),
        pytest.param(_MARKER, "", id="marker-at-eof"),
    ],
)
def test_review_output_preserves_final_review(tmp_path: Path, raw: str, expected: str) -> None:
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["review"]["steps"] if s.get("id") == "polly")
    # Replay the workflow from captured CLI stdout through GITHUB_OUTPUT.
    script = step["run"][step["run"].index('python3 -c "') :]
    output = tmp_path / "polly_output.txt"
    output.write_text(raw)
    script = script.replace("/tmp/polly_output.txt", str(output))
    github_output = tmp_path / "github_output"
    (tmp_path / "python3").symlink_to(sys.executable)
    env = {
        "PATH": f"{tmp_path}{os.pathsep}{os.defpath}",
        "GITHUB_OUTPUT": str(github_output),
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", script],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if not expected:
        assert result.returncode != 0
        assert "Polly produced no publishable review" in result.stderr
        assert not github_output.exists()
        assert not output.read_text().strip()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == expected
    header, payload = github_output.read_text().split("\n", 1)
    assert header.startswith("review_text<<")
    delimiter = header.removeprefix("review_text<<")
    assert payload == f"{expected}{delimiter}\n"
