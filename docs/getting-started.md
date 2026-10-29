# Getting started

MetaHub has a control plane for organization configuration and a data plane that receives OpenAI-compatible model requests. A typical installation uses Docker Compose, PostgreSQL, and a private state directory.

1. Prepare Ubuntu, a domain, HTTPS, backups, and provider credentials.
2. Download a signed GitHub Release and verify its checksum and signature.
3. Create a private state directory and run the production installer.
4. Create the SaaS platform administrator from an interactive terminal by running `scripts/deploy/register-saas-admin.sh`.
5. Configure providers, public model aliases, route candidates, projects, applications, and virtual keys. One application key can then call multiple aliases.
6. Send a small test request through the gateway before inviting users.

Never paste production credentials into issues, logs, screenshots, or public configuration files.
