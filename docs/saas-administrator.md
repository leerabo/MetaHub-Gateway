# SaaS platform administrator

The SaaS platform administrator is the first account created for a fresh MetaHub installation. This account manages the platform itself: organizations, platform settings, provider onboarding, release operations, and support controls. It is separate from an organization's members and from the virtual keys used by applications.

## Create the administrator

Run this once after the database migration has completed:

```bash
sudo scripts/deploy/register-saas-admin.sh /opt/metahub-state
```

The command is an operator-facing alias for the underlying verified flow:

```bash
sudo scripts/deploy/metahubctl.sh first-owner /opt/metahub-state
```

It must run in an interactive terminal. It asks for:

1. The administrator email address.
2. A display name.
3. A password of at least 8 characters, entered twice.

The command creates the first platform account, password credential, active platform operator, `platform_owner` role binding, bootstrap completion record, and audit event in one transaction. Passwords and enrollment material are not written to container logs. MFA is not required during installation; the administrator can enroll it later from account security.

## Important lifecycle rules

- This is a one-time bootstrap for an empty database. It refuses to run when identities or business data already exist.
- It is not an HTTP endpoint and cannot be triggered by a browser request.
- It does not create provider API keys, organization members, project keys, or application keys. Configure those after signing in to the platform control plane.
- If the command reports `password_too_short`, rerun it with a password of at least 8 characters. If it reports `database_not_empty` or `bootstrap_closed`, do not delete the database; use the existing account and normal account recovery flow.

## After sign-in

Create or select an organization, then configure the organization gateway in this order:

1. Register provider endpoints and store provider credentials in the control plane.
2. Create public model aliases and their ordered candidates.
3. Choose fixed-order, availability-first, cost-first, or quality-plus-cost routing.
4. Create projects and applications, add members, and issue scoped Virtual Keys.
5. Attach organization, project, application, user, or key budgets and verify a small request.

The administrator account owns the control plane. Client applications should receive only scoped Virtual Keys; they should never receive the administrator password or upstream provider credentials.
