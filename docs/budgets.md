# Budgets and usage accounting

Budgets are hierarchical controls. MetaHub supports organization, project, application, and user/credential scopes. A request must pass every applicable scope before a provider call is started.

## Recommended hierarchy

Set the organization ceiling first, then allocate project ceilings, then application and user limits. A parent must be at least as large as the effective sum of the child limits you intend to allow; the control plane validates impossible configurations instead of silently accepting a child that can never run.

## Warning and enforcement

The warning threshold is a percentage of the configured period and amount. Warning-only rules emit an alert/event when the threshold is crossed. Enforcement rules block new calls when the remaining amount is exhausted. A call can finish slightly above a limit when usage is settled after streaming; the request record and budget ledger contain the authoritative reservation and settlement details.

Review both the usage dashboard and request logs. They distinguish estimated cost, settled cost, token counts, rejected requests, and the scope that caused a block. Configure a notification channel if operators need email or webhook alerts.

Budgets do not add together across scopes: the effective allowance is the most restrictive applicable rule. Keep a small operational reserve for retries and rounding, and lower parent limits only after checking existing child allocations.
