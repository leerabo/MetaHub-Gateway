# PRD-148 navigation session regression

The local Chromium audit reproduced a stale `PATCH /identity/preferences/navigation` request. The API correctly returned `412 identity.revision_conflict`, but the previous dependency marked every error response for identity-cookie deletion. That turned a recoverable preference conflict into a browser logout and caused subsequent console requests to return `401`.

The dependency now uses an auth-error-only marker. Identity and Workspace cookies are deleted for a `401` authentication failure; the logout route keeps its existing all-outcome cleanup behavior. A targeted PRD-146 integration regression passes with no `Set-Cookie` headers on the stale revision response.

Current-source browser evidence is recorded in `outputs/prd148-navigation-cookie-regression-20260919.json`. The scenario is targeted evidence only and does not certify PRD-148 GA.
