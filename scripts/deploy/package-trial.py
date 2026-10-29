"""Create a deployment source bundle from an explicit allowlist, without host data."""
from __future__ import annotations

import argparse
import hashlib
import gzip
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[2]
TREES = ("shared-sdk/python/metahub_shared", "gateway-data-plane/src", "control-plane-api/src", "infra/migrations", "infra/seeds", "infra/production", "infra/ingress", "console-web/src", "console-web/scripts", "scripts/deploy")
TEXT = {".py", ".sh", ".sql", ".json", ".js", ".mjs", ".css", ".html", ".txt", ".in", ".lock", ".yml", ".yaml", ".conf", ".example", ".md", ".service", ".timer", ".ps1", ".psm1"}
SDK_EXAMPLE_FILES = {
    "examples/python": (
        ".env.example", "README.md", "pyproject.toml",
        "src/metahub_gateway_examples/__init__.py",
        "src/metahub_gateway_examples/chat.py", "src/metahub_gateway_examples/client.py",
        "src/metahub_gateway_examples/config.py", "src/metahub_gateway_examples/embeddings.py",
        "src/metahub_gateway_examples/errors.py",
    ),
    "examples/js": (
        ".env.example", "README.md", "package.json", "package-lock.json",
        "src/chat.js", "src/client.js", "src/config.js", "src/errors.js", "src/stream.js",
    ),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default="2.0.0-trial.45")
    args = parser.parse_args()
    import re
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?", args.version):
        raise ValueError("Invalid release version")
    files = [ROOT / ".dockerignore", ROOT / "console-web/package.json", ROOT / "console-web/production-surface-manifest.json", *ROOT.glob("console-web/*.html")]
    # Keep the public operator documentation in the same signed bundle as the
    # deployment entrypoints. The old docs/operations tree was retired from
    # the public repository; never leave stale paths in the release allowlist.
    files.extend(path for path in (ROOT / "docs").rglob("*.md") if path.is_file())
    # SDK metadata and controlled downloads validate these exact runtime files.
    # Never recursively copy example trees: they can contain local .env or keys.
    for directory, names in SDK_EXAMPLE_FILES.items():
        for name in names:
            path = ROOT / directory / name
            if not path.is_file() or any(
                parent.is_symlink() for parent in (path, *path.parents) if parent != ROOT
            ):
                raise ValueError("SDK runtime artifact missing or linked")
            files.append(path)
    for name in TREES:
        files.extend(path for path in (ROOT / name).rglob("*") if path.is_file() and not path.is_symlink() and not {"__pycache__", "tests", ".pytest_cache"}.intersection(path.parts) and (path.suffix in TEXT or path.name in {"Caddyfile", "python.Dockerfile", "console.Dockerfile"}))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    records = []
    import io
    with args.output.open("wb") as target, gzip.GzipFile(fileobj=target, mode="wb", filename="", mtime=0, compresslevel=6) as compressed, tarfile.open(fileobj=compressed, mode="w") as archive:
        for path in sorted(set(files)):
            relative = path.relative_to(ROOT).as_posix()
            data = path.read_bytes().replace(b"\r\n", b"\n")
            entry = tarfile.TarInfo(relative)
            entry.size = len(data)
            entry.mode = 0o755 if path.suffix == ".sh" else 0o644
            entry.mtime = 0
            archive.addfile(entry, io.BytesIO(data))
            records.append({"path": relative, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        manifest = json.dumps({"release": args.version, "files": records}, indent=2).encode()
        entry = tarfile.TarInfo("release-manifest.json")
        entry.size = len(manifest)
        archive.addfile(entry, io.BytesIO(manifest))
    checksum = hashlib.file_digest(args.output.open("rb"), "sha256").hexdigest()
    args.output.with_suffix(args.output.suffix + ".sha256").write_text(f"{checksum}  {args.output.name}\n")
    print(json.dumps({"package": str(args.output), "files": len(records), "bytes": args.output.stat().st_size, "sha256": checksum}))


if __name__ == "__main__":
    main()
