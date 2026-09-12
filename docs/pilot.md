# RunbookSignal 30-day pilot guide

RunbookSignal pilots should validate whether recurring-incident signals and reviewed operational knowledge improve a real support process. A pilot is not evidence of autonomous remediation, guaranteed savings, a service level, or a security/compliance certification.

## Entry prerequisites

Name accountable owners before loading pilot data:

| Owner | Required decision |
|---|---|
| Executive or service owner | Pilot scope, desired evidence, stop conditions, and final go/no-go |
| IT operations lead | Included queues, escalation process, and operator availability |
| Knowledge approver | Who may approve/revoke runbooks and how source tickets are reviewed |
| Identity administrator | Trusted issuer, confidential client, exact callbacks, tenant claim, and role assignments |
| Security/privacy owner | Allowed data classes, model-provider approval, retention/deletion, logs, backups, and incident response |
| Technical operator | PostgreSQL/Redis operation, secrets, monitoring, restore test, upgrades, and user support |

Before day 1:

- Select a dedicated pilot company/tenant and users; do not mix unrelated customers in one claim.
- Decide which ticket fields are allowed. Exclude passwords, access tokens, recovery codes, private keys, regulated data, and unrelated personal data.
- Configure a trusted OIDC issuer and administratively controlled flat `tenant_id` and `roles` claims.
- Keep `AI_ENABLED=false` until the privacy/security owner approves the provider, endpoint, models, and transmitted fields.
- Define retention and deletion for tickets, runbooks/embeddings, audit/job metadata, logs, exports, and backups.
- Test restore, tenant isolation, role boundaries, health, and the explicit resolution workflow in a non-production environment.
- Agree how pilot metrics will be collected and reviewed. The application does not calculate every metric below.

## Discovery questions

### Support workflow

- Which queues contain repeated issues that operators currently recognize manually?
- What ticket volume and time window are meaningful enough to call a pattern recurring?
- Who owns triage, escalation, knowledge review, approval, revocation, and resolution confirmation?
- What evidence must an operator record before an incident is considered resolved?
- When must a recommendation be ignored or escalated?

### Knowledge and model use

- Which existing runbooks are approved, current, and safe to recommend?
- What source evidence is required before drafting or updating a runbook?
- Which model provider and regions are approved for the allowed ticket fields?
- How will low confidence, malformed output, provider downtime, and retries be reviewed?
- Who can stop model processing, revoke knowledge, or disable a tenant?

### Identity, data, and operations

- Which issuer, client, audience, tenant claim, and role mapping are authoritative?
- Are tenant and privileged-role attributes administrator-controlled and present in both required token types?
- What are token/session lifetimes and urgent revocation expectations?
- Where will PostgreSQL, Redis, secrets, backups, and logs run, and who can access them?
- What deletion, legal-hold, incident-response, recovery, and change-approval procedures apply?

### Decision criteria

- Which baseline period will be compared with the pilot?
- What minimum evidence would justify continuing, changing scope, or stopping?
- Which outcomes require manual validation rather than dashboard counts?
- Which adverse signals—cross-tenant exposure, unsafe advice, missing approvals, unresolved failures—stop the pilot immediately?

## Thirty-day rollout

### Preparation

- Record the reviewed image digest, schema revision, configuration owners, and rollback procedure.
- Provision the pilot tenant through the operator CLI before assigning matching IdP claims.
- Test requester, viewer, agent, and admin behavior, including requester-plus-viewer write restrictions.
- Verify forced RLS with the non-owner runtime role and a two-tenant negative test.
- Load only approved pilot data. Import legacy knowledge as draft; legacy “resolutions” remain unverified recommendations.
- Exercise classification, escalation, draft review, approval/indexing, revocation, failure/retry, and explicit confirmed resolution.

### Days 1–7

- Begin with a narrow queue and named operators.
- Review every generated draft and every recommendation used in the pilot.
- Sample source tickets for data-minimization and tenant accuracy.
- Record false groupings, missed groupings, low-confidence routes, model failures, and operator overrides.
- Confirm that recommendation events are not reported as resolved outcomes.

### Days 8–21

- Review recurring clusters and knowledge gaps at least weekly.
- Revoke stale or unsafe runbooks; keep the reason in the operating record.
- Compare confirmed outcomes with the agreed baseline, controlling for queue/scope changes.
- Review access, audit events, queue health, retries, backup completion, and restore readiness.
- Stop expansion if data handling, identity claims, or tenant isolation are not demonstrably correct.

### Days 22–30

- Reconcile every claimed outcome with ticket-level evidence.
- Separate product-observed counts from manually verified pilot conclusions.
- Document unsafe or unhelpful recommendations, unresolved failures, operator burden, and missing integrations.
- Decide to stop, extend with revised criteria, or prepare a production plan. A pilot pass does not itself establish production readiness.

## Success measures

Set targets with the pilot owner; this repository does not invent them.

| Measure | Conservative definition | Evidence and caution |
|---|---|---|
| Recurring-signal coverage | Qualifying recurring patterns reviewed by an operator ÷ qualifying patterns in the agreed scope | The insight score is a prioritization heuristic, not causal root-cause proof. |
| Approved knowledge coverage | Qualifying patterns with at least one currently approved runbook ÷ qualifying patterns | A draft or indexing job is not approved knowledge. |
| Knowledge review cycle | Time from a documented knowledge gap to human approval or an explicit reject decision | Separate generation time from human review time. |
| Recommendation-assisted confirmed resolution | Tickets that received approved-runbook advice and later received an explicit verified resolution ÷ tickets that received advice | This shows sequence, not that advice caused the outcome or eliminated labor. |
| Confirmed deflection | Recommended tickets with an explicit verified resolution **and separately validated evidence that no escalation or support handoff was required** ÷ eligible pilot tickets | RunbookSignal does not currently collect enough structured evidence to calculate this automatically. Recommendation count and dashboard resolution rate are not deflection. |
| Retry recovery | Failed pilot jobs/tickets that later completed after an explicit reviewed retry ÷ reviewed retry attempts | Recovery does not erase the initial failure or imply exactly-once model billing. |
| Recurrence change | Change in ticket count for a stable pattern versus the agreed baseline window | Report scope, seasonality, outages, staffing, and other confounders; do not claim causality. |
| Safety and isolation | Confirmed unauthorized cross-tenant disclosures, privilege bypasses, or unreviewed recommendations | Define the acceptable count before launch; any event requires incident handling, not metric averaging. |

## Security and data-handling boundaries

- Authentication cannot be disabled. Tenant and roles come only from validated signed claims issued by one configured issuer.
- Application filters and PostgreSQL forced RLS protect tenant-owned tickets, runbooks, and audit records. The shared runtime credential remains a trusted application boundary.
- Browser sessions and shared rate limits use Redis; tickets, runbooks, audits, and durable jobs use PostgreSQL.
- AI is explicit opt-in. With AI disabled, intake routes to manual handling and no external model key is required.
- Model input includes ticket/runbook text described in the environment guide. Approve provider terms, geography, retention, and access before enabling it.
- Logs omit ticket bodies, OAuth codes, credentials, and exception messages, but operators still need access control, retention, monitoring, and incident procedures.
- Compose binds the bundled HTTP gateway to loopback. A production deployment still needs an operated TLS edge, secrets management, backups, monitoring, upgrades, and measured capacity.
- The synthetic local demo is not approved for real company data and is not a shortcut to production identity or model configuration.

## Known limits and non-claims

RunbookSignal currently:

- recommends reviewed instructions but does not execute remediation;
- records escalation targets but does not send notifications or prove delivery;
- groups exact model-produced category/subcategory labels rather than performing causal incident diagnosis;
- requires human approval before generated knowledge becomes eligible for recommendation;
- requires explicit operator confirmation for resolution;
- does not prove that a recommendation caused an outcome or removed human effort;
- does not provide a built-in production IdP, TLS edge, secret manager, retention scheduler, SIEM/help-desk/device-management integration, multi-host HA, or disaster-recovery service;
- does not establish pricing, savings, customer results, uptime/response SLAs, certifications, or regulatory compliance; and
- does not make the demo's Keycloak users, public fixture credentials, or deterministic model stub suitable for deployment.

Use the [deployment guide](deployment.md) and [environment reference](environment.md) for the technical baseline. Record pilot-specific decisions and evidence outside source control when they contain customer or security-sensitive information.
