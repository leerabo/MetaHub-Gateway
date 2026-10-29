# OpenAPI contracts

This directory contains the shared API contract surface used by the control
plane and gateway. The public request surface is documented in the repository
guides and exposed through the OpenAI-compatible gateway endpoints; health and
version endpoints are available for deployment checks.

When adding a public endpoint, update its typed contract, implementation,
authentication scope, and an integration test together. Do not publish private
provider credentials or customer data in generated contract examples.
