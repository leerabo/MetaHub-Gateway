"""Resolve official base image manifests; commit the resulting immutable lock."""
import json
import argparse
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
IMAGES = {
    "python": ("library/python", "3.12-slim-bookworm"),
    "node": ("library/node", "22-alpine"),
    "nginx": ("library/nginx", "1.28-alpine"),
    "postgres": ("library/postgres", "17-bookworm"),
    "caddy": ("library/caddy", "2-alpine"),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", default="2.0.0-trial.45")
    args = parser.parse_args()
    locked = {}
    for name, (repo, tag) in IMAGES.items():
        token = json.load(urllib.request.urlopen(
            "https://auth.docker.io/token?service=registry.docker.io&scope=repository:" + repo + ":pull", timeout=60
        ))["token"]
        req = urllib.request.Request("https://registry-1.docker.io/v2/" + repo + "/manifests/" + tag, headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
        })
        with urllib.request.urlopen(req, timeout=60) as result:
            digest = result.headers["Docker-Content-Digest"]
            manifest = json.load(result)
        platforms = sorted({m["platform"]["architecture"] for m in manifest.get("manifests", []) if m.get("platform", {}).get("os") == "linux"})
        locked[name] = {"image": repo.removeprefix("library/") + ":" + tag + "@" + digest, "platforms": platforms}
        print(name, digest, flush=True)
    directory = ROOT / "infra/production"
    directory.mkdir(exist_ok=True)
    (directory / "images.lock.json").write_text(json.dumps({"release": args.release, "images": locked}, indent=2) + "\n")


if __name__ == "__main__":
    main()
