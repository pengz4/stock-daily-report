from pathlib import Path

import yaml


def _workflow():
    workflow_path = (
        Path(__file__).parents[1] / ".github" / "workflows" / "daily-report.yml"
    )
    return yaml.safe_load(workflow_path.read_text(encoding="utf-8"))


def test_daily_workflow_serializes_the_full_deployment_lifecycle():
    workflow = _workflow()

    assert workflow["concurrency"] == {
        "group": "stock-daily-report-pages",
        "cancel-in-progress": False,
        "queue": "max",
    }
    assert workflow["jobs"]["generate"]["concurrency"] == {
        "group": "stock-daily-report-history",
        "cancel-in-progress": False,
        "queue": "max",
    }
    assert workflow["jobs"]["deploy"]["needs"] == "generate"
    assert workflow["jobs"]["notify"]["needs"] == ["generate", "deploy"]


def test_daily_workflow_passes_repository_configuration_paths():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert "--settings config/settings.yaml" in command
    assert "--watchlist config/watchlist.yaml" in command
    assert "--market-scan-settings config/market_scan.yaml" in command


def test_daily_workflow_reuses_an_existing_immutable_report():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert 'report_path="reports/${report_date}/report.json"' in command
    assert 'snapshot_path="snapshots/${report_date}/input.json"' in command
    assert (
        "Report already exists with current scan; reusing immutable artifacts"
        in command
    )


def test_daily_workflow_regenerates_when_a_new_scan_is_available():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert 'scan_path="market-scans/${report_date}/scan.json"' in command
    assert "market_rankings" in command
    assert "--reuse-existing-snapshot" in command
    assert "Report already exists with current scan; reusing immutable artifacts" in command


def test_daily_workflow_reuse_requires_matching_market_state_fingerprint():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert "report.market_state is not None" in command
    assert "scan.market_state is not None" in command
    assert "report.market_state.rule_version == scan.market_state.rule_version" in command
    assert "rankings.config_hash == scan.config_hash" in command


def test_daily_workflow_reuse_requires_matching_market_state_identity_and_payload():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert "resolve_market_state_identity" in command
    assert "report_identity is not None" in command
    assert "scan_identity is not None" in command
    assert "report_identity == scan_identity" in command
    assert "report.metadata.report_date == scan.report_date" in command
    assert "report_state.report_date == scan.report_date" in command
    assert "load_market_scan_settings" in command
    assert "build_market_state_identity" in command
    assert "canonical_market_state_payload(report.market_state)" in command
    assert "canonical_market_state_payload(scan.market_state)" in command


def test_daily_workflow_no_scan_reuse_requires_current_market_state_identity():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert "report.market_state is not None" in command
    assert "resolve_market_state_identity" in command
    assert "report_identity is not None" in command
    assert "report.market_state.report_date == report.metadata.report_date" in command


def test_daily_workflow_compares_exact_deep_selection_metadata():
    workflow = _workflow()
    steps = workflow["jobs"]["generate"]["steps"]
    command = next(
        step["run"]
        for step in steps
        if step["name"] == "Generate report artifacts"
    )

    assert "load_watchlist" in command
    assert "stock.name" in command
    assert "stock.group" in command
    assert "expected_deep" in command
    assert "deep_metadata == expected_deep" in command
    assert 'stock.priority == "extended"' in command
    assert "pool_ok" in command


def test_etf_collection_is_nonblocking_persistent_and_invalidates_report_reuse():
    steps = _workflow()["jobs"]["generate"]["steps"]
    by_name = {step["name"]: step for step in steps}
    capture = by_name["Capture CSI300 ETF tracker"]
    assert capture["continue-on-error"] is True
    assert "etf-tracker" in capture["run"]
    assert steps.index(capture) < steps.index(by_name["Generate report artifacts"])
    assert ".report-history/etf-tracker" in by_name["Merge previous report history"]["run"]
    persist = by_name["Persist ETF tracker history"]
    assert persist["if"] == "steps.etf-tracker.outcome == 'success'"
    assert steps.index(capture) < steps.index(persist)
    assert steps.index(persist) < steps.index(by_name["Generate report artifacts"])
    assert 'cp "$source" "$target"' in persist["run"]
    assert 'add -- "etf-tracker/${report_date}/input.json"' in persist["run"]
    generate = by_name["Generate report artifacts"]["run"]
    assert generate.count(
        "report.etf_tracker == build_tracker_report(Path.cwd(), expected_report_date)"
    ) == 2


def test_etf_history_step_persists_only_validated_input_and_is_idempotent(tmp_path):
    import os
    import subprocess

    from test_etf_tracker import DAY, make_snapshot

    from stock_daily_report.etf_tracker import save_snapshot

    snapshot = save_snapshot(tmp_path, make_snapshot())
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GITHUB_TOKEN": "fixture-token",
        "ETF_REPORT_DATE": DAY.isoformat(),
    }

    def run(*args, cwd=tmp_path):
        return subprocess.run(
            args, cwd=cwd, env=env, check=True, capture_output=True, text=True,
        ).stdout.strip()

    run("git", "init", "--quiet", "--bare", str(tmp_path / "remote.git"))
    history = tmp_path / ".report-history"
    run("git", "init", "--quiet", str(history))
    run("git", "remote", "add", "origin", str(tmp_path / "remote.git"), cwd=history)
    persist = next(
        step["run"] for step in _workflow()["jobs"]["generate"]["steps"]
        if step["name"] == "Persist ETF tracker history"
    )
    run("bash", "-e", "-c", persist)
    first_commit = run("git", "rev-parse", "HEAD", cwd=history)
    assert run("git", "ls-files", cwd=history) == "etf-tracker/2026-09-04/input.json"
    archived = run(
        "git", "--git-dir", str(tmp_path / "remote.git"), "show",
        "reports-history:etf-tracker/2026-09-04/input.json",
    )
    assert archived == snapshot.read_text(encoding="utf-8").strip()
    run("bash", "-e", "-c", persist)
    assert run("git", "rev-parse", "HEAD", cwd=history) == first_commit
