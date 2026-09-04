import subprocess
import sys


def test_publish_site_stages_site_and_reports_without_private_inputs(tmp_path):
    root = tmp_path / "repository"
    (root / "site").mkdir(parents=True)
    (root / "reports/2026-09-04").mkdir(parents=True)
    (root / "snapshots/2026-09-04").mkdir(parents=True)
    (root / ".cache").mkdir()
    (root / "site/index.html").write_text("index", encoding="utf-8")
    (root / "site/styles.css").write_text("styles", encoding="utf-8")
    (root / "reports/2026-09-04/index.html").write_text("html", encoding="utf-8")
    (root / "reports/2026-09-04/report.json").write_text("{}", encoding="utf-8")
    (root / "reports/2026-09-04/report.md").write_text("markdown", encoding="utf-8")
    (root / "snapshots/2026-09-04/input.json").write_text("secret", encoding="utf-8")
    (root / ".env").write_text("WECOM_WEBHOOK_URL=secret", encoding="utf-8")
    (root / ".cache/raw.json").write_text("cached", encoding="utf-8")

    output = tmp_path / "public"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/publish_site.py",
            "--root",
            str(root),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (output / "site/index.html").read_text(encoding="utf-8") == "index"
    assert "site/index.html" in (output / "index.html").read_text(encoding="utf-8")
    assert (
        output / "reports/2026-09-04/report.json"
    ).read_text(encoding="utf-8") == "{}"
    assert not (output / "snapshots").exists()
    assert not (output / ".env").exists()
    assert not (output / ".cache").exists()


def test_publish_site_rejects_missing_site_or_reports(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    output = tmp_path / "public"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/publish_site.py",
            "--root",
            str(root),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "site" in result.stderr
