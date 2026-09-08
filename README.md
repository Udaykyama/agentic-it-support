# NeuralDesk — Agentic IT Support

A shared-company support application with OIDC sign-in, tenant-isolated tickets, reviewed runbooks, and recurring-incident insights. PostgreSQL/pgvector stores the business records and durable jobs; Redis shares browser sessions, rate limits, and worker heartbeats across replicas.

**Recommendations are not automatic fixes.** NeuralDesk does not execute commands, change devices, or contact an escalation target automatically. An agent or administrator must confirm a resolution. AI processing is disabled until explicitly enabled and approved for the data being sent.

## What it helps teams do

- **Prioritize recurring incidents and knowledge gaps.** Compare category/subcategory clusters with the preceding time window; rank unresolved workload, growth, and volume; inspect source tickets and approved/draft runbook coverage. This makes repeated support work visible before generating more documentation. Scores are explainable prioritization heuristics, not proof of a common root cause.
- **Share an instance without sharing company data.** A provisioned company and roles come from trusted, signed identity claims. Requester ownership checks and PostgreSQL forced row-level security complement application tenant scoping.
- **Keep slow model calls off the request path.** Ticket creation and its job commit together. Workers claim PostgreSQL jobs with `FOR UPDATE SKIP LOCKED`, bounded retries, leases, and fencing against stale completions.
- **Review knowledge before recommending it.** Generation produces a draft. An administrator requests approval; an embedding job must succeed before the runbook becomes approved and searchable. Administrators can revoke it. A high-confidence classification plus an approved semantic match yields a recommendation, never a confirmed resolution.
- **Operate without a model provider.** With `AI_ENABLED=false`, intake records a ticket for manual handling and its configured escalation target. No OpenAI key is required. Unclassified new tickets do not yet contribute to category/subcategory recurrence groups.

Insights default to a 14-day window and at least three matching tickets. The ranking is `3 × unresolved + 2 × positive volume increase + current volume`; missing approved coverage is flagged as a knowledge gap, including when a draft still awaits review. Each group links up to five source tickets. Draft generation requires at least three matching tickets from the last 30 days and considers at most ten; it is not an unrestricted request to generate arbitrary procedures.

## Architecture

```text
Browser / bearer API client ── OIDC provider (Entra, Okta, Keycloak, or a broker)
           │
     TLS edge in production (operator-provided)
           │
     nginx gateway ── API replicas: Gunicorn, 1 worker × 8 threads each
                           │                    │
                    PostgreSQL + pgvector      Redis
                    tickets / runbooks /       server-side sessions /
                    audit / tenants / jobs     shared limits / heartbeats
                           │
                    Worker replicas ── approved model provider (opt-in)
```

Only the gateway publishes a host port, bound to `127.0.0.1:8000`. PostgreSQL and Redis are on an internal data network; API and worker containers also have an egress-capable network. nginx dynamically resolves API replicas through Docker DNS. Compose is a **single-host deployment baseline**, not multi-host high availability or a demonstrated production SLA.

| Module | Responsibility |
|---|---|
| `api.py` | Flask factory `create_app()`, health, metrics, error handling |
| `app/config.py`, `app/auth.py` | Validated configuration, pinned-issuer OIDC, roles, CSRF |
| `app/routes.py`, `app/ingest.py` | Versioned API, validation, pagination, idempotent intake |
| `app/models.py`, `app/db.py`, `migrations/` | PostgreSQL data model, tenant transactions, forced RLS |
| `app/jobs.py`, `app/worker.py` | Transactional job queue, retries, fenced leases, shutdown |
| `app/rate_limit.py`, `app/observability.py` | Atomic Redis/Lua limits, structured logs, per-process Prometheus metrics |
| `app/classifier.py`, `app/router.py`, `app/resolver.py` | Optional classification, recommendation/escalation, human-confirmed resolution |
| `app/runbook_gen.py`, `app/runbook_kb.py`, `app/insights.py` | Draft/review workflow, pgvector search, recurrence ranking |
| `templates/`, `static/` | Authenticated dashboard workflows |
| `docker/`, `compose.yaml`, `.github/workflows/` | Container runtime, private data services, test/build/GHCR pipeline |

## Quickstart

Prerequisites: Docker Engine/Desktop with Docker Compose v2 supporting `--wait`, an OIDC client you control, and outbound access to its issuer. No authentication-disabled development mode is provided.

1. Clone and prepare local configuration:

   ```bash
   git clone https://github.com/Udaykyama/agentic-it-support.git
   cd agentic-it-support
   cp .env.example .env
   chmod 600 .env
   openssl rand -hex 32
   ```

   Run the last command separately for **each** of `POSTGRES_PASSWORD`, `APP_DB_PASSWORD`, `REDIS_PASSWORD`, `SECRET_KEY`, and `METRICS_TOKEN`, and replace their placeholders in `.env`. Use different values. The example's database/Redis URLs interpolate the hex passwords. Do not commit `.env` or use it as shell code.

2. Configure the real `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, and `OIDC_AUDIENCE`. Register the exact callback `http://localhost:8000/auth/callback`. Configure admin-controlled, flat `tenant_id` and `roles` claims in **both ID tokens and API access tokens**. See [identity setup and configuration](docs/environment.md#identity-provider-setup). The issuer must be reachable from both your browser and containers; container `localhost` is not your host.

3. Build and start:

   ```bash
   docker compose config --quiet
   docker compose up --build --detach --wait --wait-timeout 180
   docker compose ps --all
   curl --fail http://localhost:8000/health/ready
   ```

   The new PostgreSQL volume initializes a restricted `neuraldesk_app` login. The one-shot `migrate` service runs `alembic upgrade head` with a separate owner connection before API/worker startup. An exited migration container with exit code **0** is expected. Init scripts do not run again on an existing volume.

4. Provision a company as an operator:

   ```bash
   docker compose exec api flask --app api:create_app tenants create \
     --id company-a --name 'Company A' \
     --escalation-target 'helpdesk@company-a.example'
   ```

   Assign `tenant_id=company-a` and appropriate roles through your IdP's administrative controls. Then open [http://localhost:8000](http://localhost:8000) and sign in. There are no built-in users or local passwords.

5. Optionally enable AI after privacy/provider approval: set `AI_ENABLED=true`, supply `OPENAI_API_KEY`, and recreate API and worker containers with `docker compose up -d`. SDK requests time out after 40 seconds by default; job leases are 180 seconds with at most 3 attempts. Existing manually escalated tickets are not automatically reprocessed merely by changing this setting. Before later disabling AI, stop/drain workers or deliberately handle queued work: processing a queued AI job with AI disabled marks it failed.

For scaling, upgrades, legacy imports, backup/restore, and production TLS, use the [deployment guide](docs/deployment.md). All settings and secret-file alternatives are in the [environment reference](docs/environment.md).

## Authentication and roles

| Role | Scope |
|---|---|
| `requester` | Create tickets using the signed-in email; read own tickets and approved knowledge |
| `viewer` | Company-wide read access, including recurring-incident insights |
| `agent` | Company-wide read plus intake, retries, confirmed resolutions, and runbook drafting |
| `admin` | Agent capabilities plus runbook approval/revocation and audit access |

Combining `requester` and `viewer` expands visibility, not write ownership: retrying another employee's ticket still requires `agent` or `admin`.

Browser sign-in uses authorization code + PKCE through `/auth/login` and `/auth/callback`, with sessions stored in Redis. Fetch `/api/v1/me` for the principal and `csrf_token`; send `X-CSRF-Token` on browser-session mutations, including **POST** `/auth/logout`. Local logout clears the application session; it does **not** log you out of the central IdP.

API clients supply a genuine **RS256 JWT access token** issued by the configured issuer for `OIDC_AUDIENCE`. An ID token is not a substitute for an API access token. Requesters must use the email in their signed identity; agents/admins can submit on behalf of users.

Role claims are not refreshed from the IdP on every request: issued bearer tokens retain their roles until token expiration, and browser sessions retain their signed-in roles until session/token expiration or application-session invalidation. Disabling a company blocks subsequent authenticated requests immediately. See [session and access changes](docs/environment.md#session-and-access-changes) for rotation and revocation boundaries.

```bash
# Obtain ACCESS_TOKEN through your IdP's supported OAuth client flow.
# Keep it out of source control, terminal recordings, and request logs.
export ACCESS_TOKEN='replace_with_a_real_access_token'
curl --fail-with-body http://localhost:8000/api/v1/tickets \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: vpn-report-001' \
  --data '{"title":"VPN disconnects","description":"VPN disconnects after signing in.","submitter":"you@company-a.example"}'
unset ACCESS_TOKEN
```

With AI enabled, a new ticket returns **202** with a pending ticket and job; poll `/api/v1/jobs/<id>` and the ticket detail. With AI disabled it returns **201**, with `status="escalated"` and `reason="ai_disabled"` for manual handling. Ticket responses keep `recommendation` separate from the actual, human-confirmed `resolution`. Reusing an `Idempotency-Key` with the same payload for the same company and subject returns **200** and the existing ticket; changing its payload returns **409**.

## API

The public, machine-readable contract is [`GET /api/v1/openapi.json`](http://localhost:8000/api/v1/openapi.json). Business endpoints require authentication and role/ownership checks.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/v1/me` | Current identity, company, capabilities, browser CSRF token |
| POST, GET | `/api/v1/tickets` | Submit or list tickets; bounded `limit`/`offset`, optional `status` |
| GET | `/api/v1/tickets/<id>` | Visible ticket detail |
| POST | `/api/v1/tickets/<id>/resolve` | Agent/admin confirms a resolution with `{ "resolution": "..." }` |
| POST | `/api/v1/tickets/<id>/retry` | Retry failed classification |
| GET | `/api/v1/runbooks` | Paginated knowledge, with visibility determined by role |
| POST | `/api/v1/runbooks/generate` | Queue a draft from `{ "category": "network", "subcategory": "vpn_timeout" }` |
| POST | `/api/v1/runbooks/<id>/approve` | Administrator queues embedding/approval; returns 202 |
| POST | `/api/v1/runbooks/<id>/revoke` | Administrator removes a runbook from approved use |
| GET | `/api/v1/jobs/<id>` | Tenant/ownership-scoped durable job status |
| POST | `/api/v1/jobs/<id>/retry` | Agent/admin retries a failed generation job; approval retries are admin-only |
| GET | `/api/v1/stats` | Role-scoped counts and human-confirmed resolution rate |
| GET | `/api/v1/insights` | Company recurrence/knowledge gaps; `days`, `min_tickets`, `limit` |
| GET | `/api/v1/audit` | Paginated company audit events; admin-only |
| GET | `/health/live`, `/health/ready` | Liveness; PostgreSQL + Redis readiness |
| GET | `/metrics` | Prometheus metrics; separate `METRICS_TOKEN` bearer required |

Application errors use `{ "error": { "code": "...", "message": "...", "request_id": "...", "details": {} } }` (`details` is optional). Legacy unversioned business routes return explicit **410 migration errors**, not anonymous access.

## Authenticated demo

`demo.py` creates **three real, persisted synthetic VPN tickets** in the company represented by your token. Use a dedicated evaluation tenant, not a production company's history. It requires a genuine **agent/admin access token**; there is no anonymous/default identity. Run it from the checkout after installing the Python requirements as described below; it is not included in the runtime image.

Store the token in `secrets/demo_token` using your approved IdP/client tooling, then run:

```bash
chmod 600 secrets/demo_token
export NEURALDESK_URL=http://localhost:8000
export NEURALDESK_TOKEN_FILE="$PWD/secrets/demo_token"
.venv/bin/python demo.py --wait-seconds 180
unset NEURALDESK_TOKEN_FILE NEURALDESK_URL
```

Leave `NEURALDESK_TOKEN` unset when using the file. `NEURALDESK_URL` defaults to `http://localhost:8000`; `--url` overrides it. Remote origins require HTTPS, and the hostname must match the deployed public origin.

The demo honors shared rate limits, polls pending/processing tickets, and prints recurrence/knowledge-gap insights and ticket outcomes. Append `--generate-draft` to request a draft for the **highest-ranked issue in the current company**, which is not necessarily the three tickets just submitted. It prints the durable draft job ID; it never approves a runbook or confirms a resolution. Each invocation creates another three tickets.

AI-dependent classification/drafting still requires the deployment's explicit AI opt-in. In manual mode the new tickets escalate without classification, so these tickets alone will not populate recurrence groups. If processing outlasts the polling window, inspect the printed IDs in the dashboard; the demo does not cancel durable jobs.

## Validation and delivery

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Without test service URLs, PostgreSQL integration tests are explicitly skipped. To exercise forced RLS, pgvector, real Redis, competing workers, and replica behavior, configure an isolated database ending in `_test`, migrate it as an owner, and set non-owner `TEST_DATABASE_URL` plus `TEST_REDIS_URL`; see [validation details](docs/deployment.md#validation-and-ci).

CI is configured to run the unittest suite with PostgreSQL/pgvector and Redis, build the image, and smoke-test Compose health/scaling. Pull requests **do not publish**. Successful trusted pushes to `main` publish `ghcr.io/udaykyama/agentic-it-support` with `sha-<full-commit>` and `latest` tags using `GITHUB_TOKEN`. Deploy a reviewed image digest, not an assumed cloud integration. No cloud deployment or cloud credentials are configured.

## Before production

Set `APP_ENV=production` and a pinned HTTPS `PUBLIC_URL`. Production still requires an operated TLS edge, protected secrets/KMS, controlled IdP provisioning, an approved data-processing policy, encrypted and tested backups, retention/deletion procedures, monitored database/Redis availability, image/dependency updates, and measured load/SLO exercises. Compose alone supplies none of the organizational controls or multi-host HA guarantees.

Legacy SQLite databases, ChromaDB stores, and generated runbook files are **not runtime storage or image contents**. The explicit, read-only [legacy import procedure](docs/deployment.md#legacy-import) imports tickets and draft runbooks into a chosen company; old “auto-resolutions” remain unverified recommendations.

## Author

Uday Kyama — CS Senior at CSU Sacramento
