#!/usr/bin/env bash
# Public name for the one-time SaaS platform administrator bootstrap.
# Keep the actual database and TTY safeguards in metahubctl/bootstrap-owner.py.
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
state=${1:?Usage: sudo scripts/deploy/register-saas-admin.sh STATE_DIR}
exec "$script_dir/metahubctl.sh" first-owner "$state"
