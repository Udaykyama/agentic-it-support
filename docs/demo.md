# RunbookSignal synthetic local demo

This package demonstrates recurring-incident intelligence and reviewed operational knowledge without a hosted environment, external identity account, or external AI provider. It is not a production deployment: every company, identity, incident, runbook, recommendation, and resolution is fictional.

## Boundaries

- The dashboard publishes only `127.0.0.1:8000`; the local Keycloak login publishes only `127.0.0.1:8081`.
- PostgreSQL/pgvector, Redis, and the deterministic model stub have no host ports.
- Model calls stay on an internal Docker network. The stub cannot delegate to another model provider.
- Authentication remains mandatory. The demo exercises OIDC discovery, authorization code flow, S256 PKCE, state, nonce, RS256/JWKS verification, signed company/role claims, Redis sessions, and CSRF.
- The committed passwords and service values are visible demo fixtures. Never reuse them.
- The first start may download container images. After images are present, the runtime needs no external identity or AI service and creates no monthly hosting resource.

## Prerequisites

- A local Docker Engine or Docker Desktop installation.
- Docker Compose v2 with `docker compose up --wait` support.
- Free loopback ports `8000` and `8081`.
- Enough local CPU, memory, and disk for the application, PostgreSQL, Redis, Keycloak, and the small model stub.

The launcher refuses remote Docker contexts and inherited application/bootstrap variables. It always uses the fixed `runbooksignal-demo` project, `demo/demo.env`, `compose.yaml`, and `demo/compose.demo.yaml`.

## Start

From the repository root:

```bash
./demo/run-demo.sh
```

A normal start is non-destructive. It starts or repairs the containers and inserts only missing stable baseline fixtures; it does not rewrite tickets, runbooks, approvals, resolutions, or jobs changed during a presentation.

Open [http://localhost:8000](http://localhost:8000). The dashboard and sign-in page both identify the environment as synthetic. Keycloak may take longer than the other containers on the first start.

## Synthetic identities

All users share the password `RunbookSignal-Demo!`.

| Username | Company | Role | Use in the walkthrough |
|---|---|---|---|
| `northstar-requester` | Northstar Research | requester | Own-ticket intake and visibility |
| `northstar-viewer` | Northstar Research | viewer | Company-wide read-only insights |
| `northstar-agent` | Northstar Research | agent | Investigation, retries, resolution, and draft requests |
| `northstar-admin` | Northstar Research | admin | Draft review, approval/indexing, audit |
| `northstar-requester-viewer` | Northstar Research | requester + viewer | Expanded visibility without agent write authority |
| `harbor-admin` | Harbor Peak Logistics | admin | Second-company isolation check |

Every sign-in deliberately prompts for credentials so a presenter can switch identities. Application sign-out clears the RunbookSignal session; it does not claim to revoke a general-purpose identity-provider session.

## Reset before a sales walkthrough

```bash
./demo/run-demo.sh reset
```

Reset is the only destructive demo-data action. It:

1. verifies the fixed local Docker socket, project, endpoints, runtime database name/role, and exact synthetic companies;
2. stops the loopback gateway, then waits for the demo worker to drain and stop;
3. resets the internal fail-once model state;
4. replaces records and jobs belonging to the two demo companies with stable fixtures and fresh relative timestamps; and
5. restarts the worker.

It refuses a database containing an unknown company or a conflicting tenant identity. It does not read `.env`, production secrets, production Compose projects, or external database/Redis/model endpoints.

## Guided 10–15 minute scenario

### 1. Read the recurring signal as a viewer

Sign in as `northstar-viewer`.

- Confirm the header shows **Northstar Research (Synthetic demo)** and the **Viewer** role.
- Open **Recurring Issues & Knowledge Gaps**.
- Find `Network · Vpn timeout`: current volume exceeds the preceding window, unresolved work remains, and no approved runbook exists.
- Open source tickets. The evidence is ticket-level context, not proof of a shared root cause.
- Confirm ticket submission and draft-generation controls are unavailable to this read-only role.

Sign out.

### 2. Request a draft as an agent

Sign in as `northstar-agent`.

- Return to the VPN timeout insight and select **Generate draft for this pattern**.
- Watch **Background Work** reach **Completed**.
- Open the new Knowledge Base entry. Its status is **Draft**; it cannot provide recommendations.
- Review the problem, root-cause hypothesis, steps, prevention, and linked synthetic source tickets.

Sign out.

### 3. Approve and index reviewed knowledge

Sign in as `northstar-admin`.

- Open the VPN draft.
- Read the complete draft and sources, select the review confirmation, then choose **Approve and index**.
- Watch the runbook move through **Indexing for approval** to **Approved**.
- Point out that generation did not approve the content; an administrator made the review decision and background embedding had to succeed.

### 4. Produce advice without claiming resolution

Submit this ticket as the admin:

- **Title:** `VPN times out in the synthetic sales walkthrough`
- **Description:** `Synthetic report: the VPN connection times out before connecting from a managed laptop.`
- **Submitter:** `walkthrough@northstar.example.com`

Open the ticket after processing:

- Status is **Recommended** and the source is the approved VPN runbook.
- The recommendation is reviewable advice; `resolution` is still empty.
- Confirmed-resolution statistics do not move merely because advice exists.

### 5. Show a saved failure and explicit retry

Submit:

- **Title:** `VPN retry demonstration`
- **Description:** `Synthetic retry demonstration: the VPN times out and the local model must return one invalid response.`
- **Submitter:** `retry-demo@northstar.example.com`

The deterministic stub returns malformed model content once after reset. The real worker validates it, records `invalid_model_response`, and leaves the ticket **Failed** rather than inventing a result.

Open the failed ticket and select **Retry ticket processing**. The next deterministic response validates, the durable retry completes, and the approved VPN knowledge can produce a recommendation. This is controlled fixture behavior, not a claim about external provider reliability.

### 6. Confirm the actual outcome

In the recovered ticket, record:

`Synthetic operator followed the reviewed checks and verified access to a permitted test resource.`

Select **Confirm resolved**.

- Status changes to **Confirmed resolved**.
- The recommendation remains in history but is distinct from the verified resolution.
- Confirmed-resolution statistics move only now.

### 7. Verify company isolation

Sign out and sign in as `harbor-admin`.

- The company changes to **Harbor Peak Logistics (Synthetic demo)**.
- Tickets and insights show warehouse scanner data, not Northstar VPN data.
- Harbor's scanner insight has approved knowledge coverage.
- Northstar ticket/runbook references are not discoverable in Harbor's lists or direct API lookups.

Optionally sign in as `northstar-requester` to see only that requester's baseline ticket, then as `northstar-requester-viewer` to see company-wide records. The combined role can read the agent-created failed fixture but cannot retry it; viewer visibility does not grant agent write authority.

## Lifecycle and troubleshooting

```bash
./demo/run-demo.sh status
./demo/run-demo.sh logs
./demo/run-demo.sh seed   # insert only missing stable fixtures
./demo/run-demo.sh stop   # stop containers; preserve synthetic volumes
```

If start fails, check that ports `8000` and `8081` are free and that the local Docker engine has enough resources. Use `logs` for bounded container diagnostics. Use `reset` only when presenter progress may be discarded.

For a separately configured environment with a real OIDC provider and approved model policy, the root `demo.py` API client remains available. It is intentionally distinct from this self-contained package and still requires a genuine agent/admin access token.
