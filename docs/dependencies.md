# Dependencies

The repository keeps dependency declarations close to the runtime that uses them:

| Runtime | Manifest | Purpose |
| --- | --- | --- |
| Production Python services | `infra/production/requirements.in` and `infra/production/requirements.lock` | Direct versions and the hashed transitive closure for the Python 3.12 production image |
| Runtime Python images | `infra/docker/requirements.runtime.txt` | Conservative version ranges used by development and non-production images |
| Capacity runner | `infra/docker/requirements.capacity-runner.txt` | Isolated capacity and performance worker |
| MCP sandbox | `infra/docker/requirements.mcp-sandbox.txt` | Isolated tool sandbox worker |
| Assurance supervisor | `infra/docker/requirements.assurance-supervisor.txt` | Host supervisor dependency set |
| Console frontend | `console-web/package.json` and `console-web/pnpm-lock.yaml` | Node.js frontend scripts and lockfile |
| JavaScript example | `examples/js/package.json` and `examples/js/package-lock.json` | Example client dependency lockfile |
| Python examples | `examples/python/pyproject.toml` | Example client package and optional test dependencies |
| Infrastructure images | `infra/production/images.lock.json` | Pinned container image digests |

Production builds use the hashed Python lockfile and pinned image digests. Do not replace the lockfile with an unpinned install when creating a release. When dependencies change, regenerate the lockfile in the documented Python 3.12 environment, run the full test suite, and record the new release in the changelog.

The root `pyproject.toml` configures the shared test paths and static checks; service runtime dependencies are intentionally declared in the image-specific manifests above.
