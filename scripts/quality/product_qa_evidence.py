"""Bind a QA batch to source bytes and artifacts, without storing source or secrets."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "metahub.product-qa.evidence-manifest.v1"
SOURCE_PATHS = (
    "console-web", "control-plane-api", "gateway-data-plane", "shared-sdk",
    "mcp-sandbox-worker", "infra", "tests", "scripts/quality", "scripts/acceptance",
    "pyproject.toml", "uv.lock", "conftest.py", "docs/testing/product-qa/TEST-PLAN.md",
)


def source_snapshot(root: Path = ROOT) -> dict[str, Any]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(
            ["git", *args], cwd=root, stderr=subprocess.DEVNULL, timeout=30,
        )

    revision = git("rev-parse", "HEAD").decode("ascii").strip()
    names = sorted(set(git(
        "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *SOURCE_PATHS,
    ).decode("utf-8").strip("\0").split("\0")) - {""})
    digest = hashlib.sha256()
    for name in names:
        path = root / name
        if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
            raise ValueError("qa_source_outside_repository")
        # Missing tracked files are changes too, rather than an opportunity to omit them.
        value = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
        digest.update(name.encode("utf-8") + b"\0" + value.encode("ascii") + b"\n")
    return {
        "source_revision": revision,
        "source_sha256": digest.hexdigest(),
        "source_file_count": len(names),
    }


def seal_evidence(evidence_dir: Path, started: dict[str, Any]) -> dict[str, Any]:
    finished = source_snapshot()
    artifacts = {}
    for path in sorted(evidence_dir.iterdir()):
        if path.name == "evidence-manifest.json" or not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError("qa_artifact_link_not_allowed")
        artifacts[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "schema": SCHEMA,
        "source_start": started,
        "source_finish": finished,
        "source_unchanged": started == finished,
        "artifacts": artifacts,
    }


def verify_evidence(
    evidence_dir: Path, required_files: set[str], expected_source: dict[str, Any],
) -> bool:
    try:
        manifest = json.loads((evidence_dir / "evidence-manifest.json").read_text("utf-8"))
        artifacts = manifest["artifacts"]
        if (
            manifest.get("schema") != SCHEMA
            or manifest.get("source_unchanged") is not True
            or manifest.get("source_start") != expected_source
            or manifest.get("source_finish") != expected_source
            or not required_files <= artifacts.keys()
        ):
            return False
        for name, digest in artifacts.items():
            if not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}:
                return False
            path = evidence_dir / name
            if path.is_symlink() or not path.resolve().is_relative_to(evidence_dir.resolve()):
                return False
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("start", "finish"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args()
    if args.operation == "start":
        report = source_snapshot()
    else:
        if args.start is None or args.evidence_dir is None:
            parser.error("finish requires --start and --evidence-dir")
        report = seal_evidence(
            args.evidence_dir, json.loads(args.start.read_text(encoding="utf-8")),
        )
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True, indent=2)
        stream.write("\n")
    return 0 if args.operation == "start" or report["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
