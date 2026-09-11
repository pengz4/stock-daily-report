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


def test_daily_workflow_reuses_an_existing_immutable_report():
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

    assert 'reports/${{ steps.report-date.outputs.report_date }}/report.json' in command
    assert 'snapshots/${{ steps.report-date.outputs.report_date }}/input.json' in command
    assert "Report already exists; reusing immutable artifacts" in command
