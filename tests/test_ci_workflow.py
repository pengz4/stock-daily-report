from pathlib import Path

import yaml


def test_ci_workflow_runs_tests_and_lint_for_pull_requests_to_master():
    path = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = workflow.get("on", workflow.get(True))
    job = workflow["jobs"]["quality"]
    steps = job["steps"]
    install = next(step["run"] for step in steps if step["name"] == "Install dependencies")
    checks = next(step["run"] for step in steps if step["name"] == "Run lint and tests")

    assert triggers["pull_request"]["branches"] == ["master"]
    assert triggers["push"]["branches"] == ["master"]
    assert workflow["permissions"] == {"contents": "read"}
    assert job["timeout-minutes"] > 0
    assert "requirements.lock" in install
    assert ".[dev]" in install
    assert "ruff check ." in checks
    assert "pytest -q" in checks
