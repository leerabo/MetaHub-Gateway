"""Create synthetic image-local fixture data; never copy a workstation database."""

import os
import secrets
from pathlib import Path

from metahub_shared.db.migrate import apply_migrations
from metahub_shared.db.seed import apply_seed_data


def main():
    root = Path("/app/assurance")
    root.mkdir(mode=0o755, exist_ok=False)
    for name in ("ADMIN_PASSWORD", "VIRTUAL_KEY", "PROVIDER_API_KEY"):
        os.environ[f"METAHUB_SEED_{name}"] = secrets.token_urlsafe(48)
    database = root / "base.sqlite3"
    url = f"sqlite:///{database.as_posix()}"
    apply_migrations(url, migrations_dir=Path("/app/infra/migrations"))
    apply_seed_data(url, seed_file=Path("/app/infra/seeds/default_seed.json"))
    database.chmod(0o444)


if __name__ == "__main__":
    main()
