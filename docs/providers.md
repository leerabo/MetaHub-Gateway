# Providers and model routing

MetaHub separates provider credentials from the model names that applications use. Register a provider, add its API key or compatible endpoint, discover or enter the models, then publish stable internal aliases such as `fast`, `quality`, or `embedding-default`.

## Routing model

Each route can define:

- ordered candidates and fallback behavior;
- model aliases and capability requirements;
- provider health and availability checks;
- cost-aware selection using the current model price table;
- safety, quota, budget, and maintenance constraints.

When prices change, refresh the provider/model price data and re-evaluate cost-aware routes. A route should not be hard-coded to one model if the policy is intended to follow the cheapest eligible candidate.

## Provider compatibility

OpenAI-compatible endpoints can be connected without changing the client request shape. Provider-specific adapters normalize authentication, model naming, streaming, usage, and error responses. The public adapter surface is designed for providers such as OpenAI, Anthropic, Gemini, and Grok, as well as regional providers and private endpoints.

Test a provider before enabling it in a production route. Review the request log and routing explanation for the selected model, fallback attempts, latency, token usage, and estimated cost.

Keep provider credentials in the private state store. Rotate them through the control plane, scope virtual keys to the smallest useful project or application, and never commit credentials to Git.
