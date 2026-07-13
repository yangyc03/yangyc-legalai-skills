#!/usr/bin/env python3
"""Build a whitelist-only local-redaction-assistant runtime staging directory."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


RUNTIME_ENTRIES = ("SKILL.md", "agents", "scripts", "references", "assets", "configs")
BLOCKED_NAMES = {
    "redaction_batch.local.json",
    "redaction_terms.local.json",
    "file_index.local.json",
    "sensitive_mapping.local.json",
}


class PackagingError(RuntimeError):
    pass


def collect_runtime_files(source: Path) -> list[Path]:
    result: list[Path] = []
    for entry_name in RUNTIME_ENTRIES:
        entry = source / entry_name
        if not entry.exists():
            if entry_name == "SKILL.md":
                raise PackagingError("runtime_skill_missing")
            continue
        paths = [entry] if entry.is_file() else sorted(entry.rglob("*"))
        for path in paths:
            if path.is_dir():
                continue
            if path.is_symlink():
                raise PackagingError("runtime_symlink_blocked")
            if path.name in BLOCKED_NAMES or path.suffix == ".pyc" or "__pycache__" in path.parts:
                continue
            if path.name.startswith("."):
                continue
            result.append(path.relative_to(source))
    return sorted(set(result))


def package_runtime(source: Path, destination: Path) -> list[Path]:
    files = collect_runtime_files(source)
    if destination.exists() and any(destination.iterdir()):
        raise PackagingError("runtime_destination_not_empty")
    destination.mkdir(parents=True, exist_ok=True)
    for relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, target)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a whitelist-only runtime staging directory.")
    parser.add_argument("--source", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--destination", required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    source = Path(args.source).expanduser().resolve()
    destination = Path(args.destination).expanduser().resolve()
    files = collect_runtime_files(source)
    if not args.check_only:
        files = package_runtime(source, destination)
    print(f"runtime_files={len(files)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
