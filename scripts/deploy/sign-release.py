"""Sign an already reviewed deployment bundle; does not publish to GitHub."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--summary", required=True)
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9.-]+)?", args.version) or len(args.summary) > 300:
        raise ValueError("Invalid release metadata")
    if args.bundle.name != f"metahub-{args.version}-source.tar.gz":
        raise ValueError("Bundle filename must match the release version")
    manifest = args.bundle.parent / "metahub-release.json"
    signature = args.bundle.parent / "metahub-release.sig"
    if manifest.exists() or signature.exists():
        raise ValueError("Refusing to replace an existing signed release")
    manifest.write_text(json.dumps({
        "version": args.version, "summary": args.summary, "bundle": args.bundle.name,
        "bytes": args.bundle.stat().st_size,
        "sha256": hashlib.file_digest(args.bundle.open("rb"), "sha256").hexdigest(),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", str(args.key), "-rawin",
                    "-in", str(manifest), "-out", str(signature)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Release manifest and signature prepared. Nothing was published.")


if __name__ == "__main__":
    main()
