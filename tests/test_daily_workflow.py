from pathlib import Path

import yaml


def test_daily_workflow_passes_repository_configuration_paths():
    workflow_path = (
        Path(__file__).parents[1] / ".github" / "workflows" / "daily-report.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert "--settings config/settings.yaml" in command
    assert "--watchlist config/watchlist.yaml" in command
