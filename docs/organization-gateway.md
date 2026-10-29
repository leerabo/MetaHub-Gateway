# The organization gateway model

MetaHub is an organization control plane attached to a model gateway. It is designed so a team can give an application one controlled credential while administrators keep provider credentials, routing, budgets, and audit policy in one place.

## The control hierarchy

```text
Organization
  └─ Project
      └─ Application
          └─ Virtual API key
              └─ Public model aliases
                  └─ Ordered model candidates
                      └─ Provider credentials and endpoints
```

The client receives only a MetaHub Virtual Key. It never receives an OpenAI, Anthropic, Gemini, Grok, or regional provider key. A Virtual Key can be scoped to a project and application, rate-limited, disabled, rotated, and audited. The application can then use the model aliases that the organization exposes to it.

## One key, many model routes

One application key does not mean one fixed upstream model. The same key can call several public aliases, and each alias can select a different provider or model. For example, an administrator might expose:

| Public alias | Candidate route | Intended use |
| --- | --- | --- |
| `team-chat` | `gpt-6-sol` → `claude-sonnet` → `gemini-pro` | General conversation |
| `team-code` | `claude-sonnet` → `gpt-6-sol` | Code review and implementation |
| `team-fast` | `gemini-flash` → `gpt-6-luna` | Low-latency, lower-cost tasks |

The client keeps one base URL and one bearer key, and selects the organization-owned alias in the request:

```bash
export METAHUB_URL=https://gateway.example.com
export METAHUB_KEY=mhv_your_virtual_key

curl "$METAHUB_URL/v1/chat/completions" \
  -H "Authorization: Bearer $METAHUB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"team-chat","messages":[{"role":"user","content":"Summarize this incident."}]}'

curl "$METAHUB_URL/v1/chat/completions" \
  -H "Authorization: Bearer $METAHUB_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"team-code","messages":[{"role":"user","content":"Review this patch."}]}'
```

The application does not need to know which provider served either request. The request log records the requested alias, selected model, provider, fallback attempts, usage, cost, budget result, and routing explanation.

## How an administrator creates this flow

1. Create a provider entry and store its credential in the control plane.
2. Add or discover the provider models, including capability and current input/output/cache prices.
3. Create a model alias for each stable team-facing capability. The alias is the contract used by applications; upstream model names can change behind it.
4. Choose the routing mode and candidate order. A route has one default candidate and can have up to 31 ordered fallback candidates.
5. Create the project and application, add members, and assign the application key.
6. Attach budgets and rate limits at organization, project, application, user, or key scope.
7. Test the provider and send a small request with the new key before inviting users.

## Route selection modes

- **Fixed** follows the administrator's candidate order. It is useful when a workload must stay on a known model.
- **Availability first** prefers eligible and healthy candidates, then keeps the configured order as the deterministic tie-breaker.
- **Cost first** compares current prices for eligible candidates in one comparable currency. Missing prices or incomparable currencies do not become a free winner; the route falls back to the configured order and explains why.
- **Quality + cost** applies the configured quality floor and evidence sample threshold, then scores quality, cost, and latency. Cold-start and tie behavior are deterministic and visible in the routing explanation.

Fallback is a second layer. If the selected candidate fails with a retryable upstream or transport error, MetaHub can try the next candidate under the alias's fallback policy. Budget, permission, model capability, and safety blocks are evaluated before a provider call and are not silently bypassed by fallback.

## What happens during a request

For each request, the gateway evaluates the route in a stable order:

1. Authenticate the Virtual Key and resolve its organization, project, and application scope.
2. Check that the requested alias is published to that application and that the request fits its capability policy.
3. Apply organization, project, application, user, and key budget/rate limits before contacting a provider.
4. Filter disabled, unhealthy, incompatible, or missing-price candidates.
5. Select a candidate using the alias's route mode and current provider metadata.
6. Call the provider, record usage and cost, and retry only retryable failures against the configured fallback candidates.

The result is one client response, but the audit record preserves the route decision: requested alias, eligible candidates, selected provider/model, fallback attempts, latency, token usage, cost, and any budget or policy refusal. This lets an administrator change routing centrally while keeping client integrations stable and the reason for each choice reviewable.

## Governance boundaries

Provider keys belong to the organization and are never returned to clients. Virtual Keys are scoped credentials for people, projects, and applications. Disabling an application, project, member, route, or key takes effect at the gateway boundary. Usage and cost are attributed to the organization hierarchy so finance and engineering can review the same request from different views.

This is the difference between a personal forwarding proxy and an organization gateway: the endpoint is only the data plane. The durable product contract is the control plane that decides who may call, which alias they may use, which models are eligible, what the call may cost, and why a particular model was selected.
