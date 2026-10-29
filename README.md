# MetaHub — the organization AI gateway

MetaHub is the governance layer for an organization's AI traffic. It gives teams one controlled gateway key while administrators manage provider credentials, application access, budgets, and explainable multi-model routing in one place.

This is an organization gateway, not a personal forwarding endpoint. Every request is evaluated against the organization hierarchy, the application's virtual key, the requested model alias, the route policy, the applicable budget, and the audit policy before it reaches a provider.

[中文文档](README.zh-CN.md) · [Documentation](docs/README.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md)

## What users get from one key

An application receives one MetaHub Virtual Key and one base URL. That key can call several stable organization-owned model aliases:

```text
one application key
  ├─ team-chat  → fast model → primary model → backup model
  ├─ team-code  → coding model → backup model
  └─ team-fast  → lowest-cost eligible model → backup model
```

The application never receives upstream OpenAI, Anthropic, Gemini, Grok, or regional provider credentials. Administrators can change the providers, model versions, prices, and fallback order behind an alias without changing every client integration.

Routing can be fixed-order, availability-first, cost-first, or quality-plus-cost. Cost-first routes recalculate from current eligible prices; they are not permanently pinned to the model that was cheapest when the route was created. Each decision records the candidates considered, the selected model, fallback attempts, budget result, and the reason for the choice.

Read the full [organization gateway model](docs/organization-gateway.md) before treating MetaHub as a simple API proxy.

## Why organizations use MetaHub?

Teams often outgrow a collection of provider dashboards and personal API keys. MetaHub puts the control plane and the request gateway in one deployable system:

- **Organization and team management** — workspaces, members, roles, permissions, projects, and applications.
- **One key, many model routes** — an application can use several organization-owned aliases through one Virtual Key; each alias can select multiple providers and models with ordered fallback, availability, cost, and quality policies.
- **Provider governance** — organization-owned credentials and extensible adapters for OpenAI, Anthropic, Gemini, Grok, and other providers; clients see aliases, never provider secrets.
- **Virtual API keys** — scoped keys for people, projects, and applications with lifecycle and access controls.
- **Budgets and cost controls** — organization, project, application, and user budgets with thresholds, enforcement, and usage accounting.
- **Observability** — request records, token usage, estimated cost, routing explanations, provider errors, and audit events.
- **Self-hosting** — Docker Compose deployment on Ubuntu with PostgreSQL, versioned migrations, backups, and signed releases.

MetaHub exposes an OpenAI-compatible gateway surface so existing SDKs and applications can adopt it without rewriting their request code.

## Quick start

The recommended production path uses a versioned GitHub Release. Do not run `git pull` in production and do not store database volumes, API keys, SMTP credentials, or `.env` files in the repository.

```bash
sudo scripts/deploy/metahubctl.sh install /opt/metahub-state
sudo scripts/deploy/register-saas-admin.sh /opt/metahub-state
```

Replace the state path with your private deployment location. The complete workflow is documented in [Ubuntu deployment](docs/deployment/ubuntu.md), including HTTPS, SMTP, backups, upgrades, and rollback.

## Documentation

- [Getting started](docs/getting-started.md)
- [The organization gateway model](docs/organization-gateway.md)
- [SaaS platform administrator](docs/saas-administrator.md)
- [Ubuntu deployment](docs/deployment/ubuntu.md)
- [Configuration reference](docs/configuration.md)
- [Providers and model routing](docs/providers.md)
- [Budgets and usage accounting](docs/budgets.md)
- [Upgrades and rollback](docs/upgrades.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Changelog](CHANGELOG.md)

## Development

```bash
npm --prefix console-web install
npm --prefix console-web test
npm --prefix console-web run lint -- --no-fix
npm --prefix console-web run build
```

See [Contributing](CONTRIBUTING.md) before opening a pull request.

## License

MetaHub is released under the [MIT License](LICENSE).
