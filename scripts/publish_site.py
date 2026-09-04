"""Stage only public report artifacts for GitHub Pages."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path


class PublishSiteError(RuntimeError):
    """Raised when the Pages artifact cannot be staged safely."""


def stage_pages(root: str | Path, output: str | Path) -> None:
    """Copy site and report artifacts, excluding snapshots, cache, and secrets."""

    source_root = Path(root).expanduser().resolve()
    output_root = Path(output).expanduser().resolve()
    site_root = source_root / "site"
    reports_root = source_root / "reports"
    if not site_root.is_dir():
        raise PublishSiteError(f"required site directory is missing: {site_root}")
    if not reports_root.is_dir():
        raise PublishSiteError(f"required reports directory is missing: {reports_root}")
    if output_root == source_root:
        raise PublishSiteError("output directory must differ from repository root")
    if output_root.exists():
        if not output_root.is_dir():
            raise PublishSiteError(f"output path is not a directory: {output_root}")
        if any(output_root.iterdir()):
            raise PublishSiteError(f"output directory must be empty: {output_root}")
    else:
        output_root.mkdir(parents=True)

    _copy_public_tree(site_root, output_root)
    _copy_public_tree(reports_root, output_root / "reports")


def _copy_public_tree(source: Path, destination: Path) -> None:
    _reject_symlinks(source)
    destination.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir()):
        if child.name.startswith("."):
            continue
        target = destination / child.name
        if child.is_dir():
            _copy_public_tree(child, target)
        elif child.is_file():
            shutil.copy2(child, target)
        else:
            raise PublishSiteError(f"unsupported artifact entry: {child}")


def _reject_symlinks(source: Path) -> None:
    if source.is_symlink():
        raise PublishSiteError(f"symlinked artifact source is not allowed: {source}")
    for child in source.rglob("*"):
        if child.is_symlink():
            raise PublishSiteError(f"symlinked artifact source is not allowed: {child}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="publish_site")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        stage_pages(args.root, args.output)
    except (OSError, PublishSiteError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
