# Environment and identity configuration

[`app/config.py`](../app/config.py) is the source of truth. Configuration is validated when the Flask factory starts; there is no import-time application and no authentication-disabled mode. [`compose.yaml`](../compose.yaml) passes application settings explicitly, rather than exposing the entire bootstrap `.env` to API/worker containers.

Copy [`.env.example`](../.env.example) to `.env` for local evaluation. **Replace all placeholders before the first start.** Protect it with `chmod 600 .env`; never commit it, paste it into an issue, print a resolved Compose configuration into CI logs, or `source` it as executable shell input. `docker compose config --quiet` checks configuration without printing resolved credentials.

## Application settings

Defaults below are the **application** defaults. Compose deliberately sets `TRUSTED_PROXY_HOPS=1`; `.env.example` explicitly selects `APP_ENV=development`. Neither change disables authentication.

| Variable | Default | Meaning / constraints |
|---|---|---|
| `APP_ENV` | `production` | `production`, `development`, or `test`. Production requires HTTPS origins and secure browser cookies. `test` is for isolated test fixtures, not deployment. |
| `DATABASE_URL` | Required | Runtime `postgresql+psycopg://neuraldesk_app:...@db:5432/neuraldesk`. Must use a non-owner login without `SUPERUSER` or `BYPASSRLS`. SQLite is accepted only for unit tests under `APP_ENV=test`. Supports `_FILE`. |
| `REDIS_URL` | Required | `redis://` or `rediss://` URL for shared sessions, limits, and heartbeats. Use authenticated Redis; use TLS for remote/untrusted transport. Supports `_FILE`. |
| `SECRET_KEY` | Required | Independently generated random secret, at least 32 characters; `change...` placeholders are rejected. Supports `_FILE`. |
| `PUBLIC_URL` | `http://localhost:8000` | Pinned external origin, no path, credentials, query, or fragment. **Set an HTTPS origin in production**; the HTTP default is not production-valid. Its hostname is the Flask trusted host, and callbacks use this origin rather than forwarded host headers. |
| `OIDC_ISSUER` | Required | One pinned issuer URL; HTTPS in production. Must match the discovery/token issuer **exactly, including any trailing slash**; the configured issuer is not normalized. |
| `OIDC_CLIENT_ID` | Required | Registered confidential browser client; ID-token audience. |
| `OIDC_CLIENT_SECRET` | Required | Client credential issued by your IdP. Required even when AI is off. Supports `_FILE`. |
| `OIDC_AUDIENCE` | Required | Expected API **access-token** audience; may differ from the browser client ID. |
| `OIDC_TENANT_CLAIM` | `tenant_id` | Literal, flat signed claim key containing the provisioned company ID. Not a JSONPath or an automatically traversed nested key. |
| `OIDC_ROLES_CLAIM` | `roles` | Literal signed claim key containing a nonempty array of application role strings. |
| `METRICS_TOKEN` | Required | Independently generated bearer secret, at least 32 characters, for `/metrics`. It is not an OIDC token. Supports `_FILE`. |
| `AI_ENABLED` | `false` | Exactly `true` or `false` (case-insensitive). Explicit consent to model processing; when false, intake is manual and AI-dependent operations are unavailable. |
| `OPENAI_API_KEY` | Empty | Required **only** when `AI_ENABLED=true`. Use a provider credential approved for this data. Supports `_FILE`. |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base; HTTPS in production, no URL credentials/query/fragment. A different endpoint must implement the required classification and embeddings APIs; compatibility is not assumed. |
| `CLASSIFICATION_MODEL` | `gpt-4o-mini` | Model used for classification and runbook drafting. Revalidate structured output and quality before changing it. |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Model compatible with the fixed **1536-dimensional** vector schema. Do not mix embeddings from different models; changing it requires a planned re-embedding/review migration. |
| `AI_TIMEOUT_SECONDS` | `40` | Integer `1–120`, per SDK request. Durable job retries, not unbounded SDK waits, handle failure. |
| `CONFIDENCE_THRESHOLD` | `0.75` | Float `0–1`; classification threshold for considering a recommendation. Not a verified success probability. |
| `RUNBOOK_SIMILARITY_THRESHOLD` | `0.80` | Float `0–1`; minimum cosine similarity against eligible approved runbooks. |
| `SESSION_LIFETIME_SECONDS` | `3600` | Integer `60–28800`. Browser sessions are also bounded by token expiry; active browsing does not provide an unlimited sliding lifetime. |
| `JOB_LEASE_SECONDS` | `180` | Integer `30–900`; must be at least `2 * AI_TIMEOUT_SECONDS + 30`. Allows bounded calls and finalization before a stale job is reclaimed. |
| `JOB_MAX_ATTEMPTS` | `3` | Integer `1–10`; maximum durable processing attempts before terminal failure. |
| `WORKER_POLL_SECONDS` | `2` | Float `0.1–60`; idle polling interval. Keep this below the job lease/heartbeat lifetime when tuning. |
| `RATE_LIMIT_PER_MINUTE` | `120` | Integer `1–10000`; shared principal request limit. An additional IP admission limit is five times this setting. |
| `AI_RATE_LIMIT_PER_MINUTE` | `10` | Integer `1–1000`; shared company limit for AI work requests. This is not a provider-spend cap. |
| `DATABASE_POOL_SIZE` | `5` | Integer `1–50`, per process. SQLAlchemy also permits **5 overflow connections per process**; account for every replica and worker health probe. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` (case-insensitive). Never enable third-party HTTP/body logging around real tickets or OAuth tokens. |
| `TRUSTED_PROXY_HOPS` | `0` | Integer `0–3`; number of trusted forwarded-for/proto hops. Supplied Compose uses **1** because the gateway replaces these headers. Use **0** when serving directly; never accept direct untrusted access with a nonzero value. |

`PUBLIC_URL`, `OIDC_ISSUER`, and `OPENAI_BASE_URL` must be absolute HTTP(S) URLs without credentials, query strings, or fragments. HTTP is allowed in development/test only. Changing configuration requires recreating the relevant containers; files are read at process startup, not continuously watched.

## Migration and Compose bootstrap variables

These are separate from application settings. API and worker services **do not receive** the migration-owner credentials or the bootstrap password variables.

| Variable | Default / example | Purpose |
|---|---|---|
| `MIGRATION_DATABASE_URL` | Required by migration service | Owner connection, e.g. `postgresql+psycopg://postgres:...@db:5432/neuraldesk`. Must create the extension/tables/policies. Alembic can fall back to `DATABASE_URL` outside Compose, but using the runtime role for migrations is intentionally unsuitable. Supports `_FILE`. |
| `COMPOSE_PROJECT_NAME` | `neuraldesk` in `.env.example` | Stable Compose project/volume namespace. Use a different project for isolated environments; changing it points to different named volumes. |
| `NEURALDESK_IMAGE` | `neuraldesk:local` | Shared API/worker/migration image. Set a reviewed GHCR image **digest** when deploying a published build. |
| `POSTGRES_DB` | `neuraldesk` | Database initialized by the PostgreSQL image on a new volume. Keep URL database names consistent. Use a name ending in `_test` for integration tests. |
| `POSTGRES_USER` | `postgres` | Bootstrap/migration owner, never the runtime login. Keep the owner consistent with default privileges and restore procedures. |
| `POSTGRES_PASSWORD` | Required | Owner/bootstrap password; not an API credential. Generate an independent 64-character hex value. The official image supports `_FILE`. Changing this variable does not change an existing database password. |
| `APP_DB_PASSWORD` | Required | Password used **once on a new database volume** to create `neuraldesk_app`. Generate a separate 64-character hex value and keep `DATABASE_URL` in sync. The init script supports `_FILE`. |
| `REDIS_PASSWORD` | Required | Bootstrap password used to generate a protected, in-memory Redis configuration. The supplied entrypoint requires **hexadecimal, at least 32 characters**; `openssl rand -hex 32` generates 64. Keep `REDIS_URL` in sync. Supports `_FILE`. |

Compose mounts named `postgres_data` and `redis_data` volumes, normally prefixed with the project name. PostgreSQL initialization and role creation run only when the data volume is new; see [credential rotation](deployment.md#credential-rotation) before changing an existing deployment.

### Password and URL generation

Run this separately for each password, session secret, and metrics token:

```bash
openssl rand -hex 32
```

Do not reuse secrets. Hex avoids connection-URL quoting problems involving `@`, `:`, `/`, `#`, `%`, and `$`. If integrating an externally generated non-hex database credential, percent-encode its username/password components before constructing a SQLAlchemy URL. The bundled Redis bootstrap deliberately accepts only hex; an externally managed Redis service can use its own authenticated URL.

`.env.example` uses Compose/dotenv interpolation for convenience:

```dotenv
DATABASE_URL=postgresql+psycopg://neuraldesk_app:${APP_DB_PASSWORD}@db:5432/${POSTGRES_DB}
REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
```

A mounted URL secret must instead contain the **fully resolved URL**; the application does not interpolate `${...}` inside secret files.

## Local demo and integration-test settings

These variables are consumed by `demo.py` or the test suite, not by `Settings` or the deployed API/worker. Compose does not pass demo tokens into application containers.

| Variable | Default | Purpose |
|---|---|---|
| `NEURALDESK_URL` | `http://localhost:8000` | Demo API origin; `--url` overrides it. Must be HTTPS except for loopback HTTP, with no path/credentials/query/fragment. Use the deployment's trusted public hostname. |
| `NEURALDESK_TOKEN` | Required unless file supplied | Genuine OIDC **agent/admin access token** for the provisioned evaluation company. Not an ID token, metrics token, or application secret. Prefer the file alternative; do not retain tokens in shell history or logs. |
| `NEURALDESK_TOKEN_FILE` | Unset | Host-side UTF-8 file containing the demo access token; surrounding whitespace is stripped. Leave `NEURALDESK_TOKEN` unset/blank when using this. Protect its ownership/permissions and never commit it. |
| `TEST_DATABASE_URL` | Unset; PostgreSQL tests skip | Isolated `postgresql+psycopg://` **runtime non-owner** connection whose database name ends in `_test`. Run migrations separately as an owner first. CI uses `neuraldesk_test`; never point tests at production. |
| `TEST_REDIS_URL` | Unset; PostgreSQL tests skip | Real, disposable/isolated Redis endpoint for integration tests. Both test URLs must be present for this coverage to run. CI uses a runner-only Redis service and database 15. |

The demo loads `.env` without overriding already exported variables. Its `--wait-seconds` argument is positive and defaults to `180`; request timeouts and rate-limit backoff may extend total wall-clock duration. `--generate-draft` optionally queues a draft for the company's highest-ranked issue, **never approval**. Every invocation writes three synthetic tickets. Use an evaluation tenant and see the [demo procedure](../README.md#authenticated-demo); do not assume the `_test` database guard for integration tests also applies to the HTTP demo.

## Secret files and secret managers

For the following alternatives, leave the direct value unset or blank and set only the file path. A nonempty direct value together with its `_FILE` value is rejected. File contents are read as UTF-8 and stripped of surrounding whitespace.

| File variable | Replaces |
|---|---|
| `DATABASE_URL_FILE` | `DATABASE_URL` |
| `REDIS_URL_FILE` | `REDIS_URL` |
| `SECRET_KEY_FILE` | `SECRET_KEY` |
| `OIDC_CLIENT_SECRET_FILE` | `OIDC_CLIENT_SECRET` |
| `METRICS_TOKEN_FILE` | `METRICS_TOKEN` |
| `OPENAI_API_KEY_FILE` | `OPENAI_API_KEY` |
| `MIGRATION_DATABASE_URL_FILE` | `MIGRATION_DATABASE_URL` in Alembic |
| `POSTGRES_PASSWORD_FILE` | PostgreSQL image's `POSTGRES_PASSWORD` |
| `APP_DB_PASSWORD_FILE` | PostgreSQL init script's `APP_DB_PASSWORD` |
| `REDIS_PASSWORD_FILE` | Redis entrypoint's `REDIS_PASSWORD` |

For example, clear the direct variables in the deployment `.env` and configure:

```dotenv
DATABASE_URL=
DATABASE_URL_FILE=/run/secrets/database_url
REDIS_URL=
REDIS_URL_FILE=/run/secrets/redis_url
SECRET_KEY=
SECRET_KEY_FILE=/run/secrets/secret_key
OIDC_CLIENT_SECRET=
OIDC_CLIENT_SECRET_FILE=/run/secrets/oidc_client_secret
METRICS_TOKEN=
METRICS_TOKEN_FILE=/run/secrets/metrics_token
MIGRATION_DATABASE_URL=
MIGRATION_DATABASE_URL_FILE=/run/secrets/migration_database_url
POSTGRES_PASSWORD=
POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password
APP_DB_PASSWORD=
APP_DB_PASSWORD_FILE=/run/secrets/app_db_password
REDIS_PASSWORD=
REDIS_PASSWORD_FILE=/run/secrets/redis_password
```

Add a deployment-only `compose.secrets.yaml` alongside `compose.yaml`:

```yaml
x-application-secrets: &application-secrets
  - database_url
  - redis_url
  - secret_key
  - oidc_client_secret
  - metrics_token

services:
  api:
    secrets: *application-secrets
  worker:
    secrets: *application-secrets
  migrate:
    secrets: [migration_database_url]
  db:
    secrets: [postgres_password, app_db_password]
  redis:
    secrets: [redis_password]

secrets:
  database_url:
    file: ./secrets/database_url
  redis_url:
    file: ./secrets/redis_url
  secret_key:
    file: ./secrets/secret_key
  oidc_client_secret:
    file: ./secrets/oidc_client_secret
  metrics_token:
    file: ./secrets/metrics_token
  migration_database_url:
    file: ./secrets/migration_database_url
  postgres_password:
    file: ./secrets/postgres_password
  app_db_password:
    file: ./secrets/app_db_password
  redis_password:
    file: ./secrets/redis_password
```

```bash
docker compose -f compose.yaml -f compose.secrets.yaml config --quiet
docker compose -f compose.yaml -f compose.secrets.yaml up -d --wait
```

If AI is enabled, also blank `OPENAI_API_KEY`, set `OPENAI_API_KEY_FILE=/run/secrets/openai_api_key`, define that file secret, and mount it into **both** API and worker. Continue using the same `-f` arguments for future operations.

Keep actual secret files out of Git and backups intended for source code. The Docker build context is allowlisted and excludes secrets, but that is not a substitute for repository hygiene. Compose file-backed secrets are **read-only mounts, not an encrypted secret-management service**. Protect the host and provision files so the relevant non-root container identity can read them: application UID/GID `10001:10001`, Redis `999:1000`, and the PostgreSQL image's `postgres` user for initialization. Local Compose bind-backed secrets may not honor requested `uid`/`gid`/`mode` remapping; verify real ownership/ACLs rather than relying on the YAML alone. Do not make every host user's access world-readable just to bypass an ownership problem.

A secret manager/KMS agent can materialize the same narrowly scoped files before startup, or inject environment values at deployment time. Do not bake secrets into an image/build argument. Rotation requires coordinated process recreation and, where appropriate, application-session invalidation.

## Identity provider setup

### Trust and company provisioning

One deployment trusts **one configured issuer**, discovered from `OIDC_ISSUER`, never from an unverified incoming token. For companies with different IdPs, federate through a trusted OIDC broker with centrally controlled claim mapping, or use separate application deployments. Arbitrary per-token issuer discovery is not supported.

Provision the company before allowing sign-in:

```bash
docker compose exec api flask --app api:create_app tenants create \
  --id company-a --name 'Company A' \
  --escalation-target 'helpdesk@company-a.example'
```

Company IDs are `1–128` characters, begin with a letter/digit, and then contain letters/digits, dots, underscores, or hyphens. The signed company claim must match exactly.

Configure an OIDC **confidential web client**, authorization code flow with S256 PKCE, and the exact redirect URI `${PUBLIC_URL}/auth/callback`. No wildcard callback is needed. The app requests `openid profile email`; configure necessary claims to be included without relying on an additional scope that the application does not request.

Tokens must be RS256 signed with a `kid`, valid issuer/audience/expiry, `iat`, and `sub`. Add claims like these through your IdP's **administrative** controls:

```json
{
  "tenant_id": "company-a",
  "roles": ["requester"],
  "email": "you@company-a.example"
}
```

This is an illustrative claim mapping, not a usable token or built-in identity. Browser authorization uses **ID-token** claims with audience `OIDC_CLIENT_ID`; bearer APIs use **access-token** claims with audience `OIDC_AUDIENCE`. Both must include the appropriate company and application roles. Recognized roles are `requester`, `viewer`, `agent`, and `admin`; roles combine through their capabilities. A requester needs a valid signed email to submit their own tickets.

**Never source `tenant_id` or privileged roles from user-editable profile attributes, request parameters, email-domain guesses, or unsigned data.** A signed but user-editable claim is still an authorization vulnerability. Restrict the IdP management APIs, mapping permissions, group/role assignment, and service-account grants. Company IDs are application companies, not automatically equivalent to an IdP's directory/organization ID.

### Provider examples

| Provider | Example issuer shape | Configuration notes |
|---|---|---|
| Microsoft Entra ID | `https://login.microsoftonline.com/<directory-id>/v2.0` | Pin a specific trusted issuer, not `/common`. Register the web callback and expose/configure the API audience. Administratively assign application roles and a controlled company claim for both token types; do not assume directory `tid` identifies each customer company in a shared directory. |
| Okta | `https://<org>.okta.com/oauth2/<authorization-server-id>` | Use an authorization server configured for this API audience and custom claims. Include the company/role claims in ID and access tokens. An organization-server token intended for Okta itself is not automatically an application API token. |
| Keycloak | `https://<host>/realms/<realm>` | Configure a confidential client, standard code flow, S256 PKCE, access-token audience mapping, and protocol mappers that emit **flat** company/role claims in ID and access tokens. Default nested `realm_access.roles` is not read automatically. Keep company attributes and role assignments administrator-controlled. |

Check actual issued tokens locally using your IdP's tooling without uploading real tokens to third-party decoders. Test at least two provisioned companies and all four roles. Development allows HTTP, but containers and browsers still need to reach the same issuer hostname; never replace OIDC with hardcoded test identities on a deployed instance.

### Session and access changes

Browser sessions live in Redis and expire at the shorter of token validity and configured session lifetime. `/auth/logout` is **POST with CSRF**, clearing only the application session; it does not terminate central IdP SSO or revoke issued bearer tokens.

The application validates bearer JWTs but does not perform online token introspection or fetch fresh IdP roles on each request. An issued token retains its signed roles until expiration; a browser session retains its signed-in roles until session/token expiration or application-session invalidation. Changing an IdP assignment or revoking a provider login does not by itself guarantee immediate rejection of an already issued JWT here.

Redis browser-session namespaces are bound to `SECRET_KEY` and the configured environment, issuer, client ID, audience, and claim names. Changing the key or this identity configuration makes prior browser sessions inaccessible to the new configuration; recreate **all API replicas** together so old replicas do not continue accepting the previous namespace. Old Redis entries can remain until expiry and must be handled under the retention/incident policy. This mechanism does not revoke bearer access tokens or central IdP sessions.

For an immediate company-wide block:

```bash
docker compose exec api flask --app api:create_app tenants disable --id company-a
```

Tenant enablement is checked on authenticated requests. After review, an operator can restore access:

```bash
docker compose exec api flask --app api:create_app tenants enable --id company-a
```

Treat provisioning/disable/enable CLI access as privileged host administration, not a self-service tenant API.
