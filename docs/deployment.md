# Deployment and operations

This repository supplies a cloud-neutral, **single-host Docker Compose baseline**. It does not deploy to a cloud account, provide multi-host failover, or establish production throughput/SLA claims. Start with the [README quickstart](../README.md#quickstart) and [environment reference](environment.md); the procedures below assume valid credentials and a configured OIDC client.

## Runtime and trust boundaries

| Service | Purpose | Network / persistence |
|---|---|---|
| `gateway` | nginx 1.28, dynamic API upstreams, forwarded-header replacement | Egress-capable `edge`; only host publication, **`127.0.0.1:8000 → 8080`** |
| `api` | `gunicorn ... 'api:create_app()'`, **one worker and eight threads** per container | `edge` + internal `data`; no published port; read-only root filesystem |
| `worker` | `python -m app.worker` | `edge` + `data`; no ports; read-only root filesystem |
| `migrate` | One-shot `alembic upgrade head` as migration owner | `data` only; no runtime secrets except its owner connection |
| `db` | PostgreSQL 16 with pgvector | `data` only, **no published port**; named `postgres_data` volume |
| `redis` | Authenticated Redis 7.4 with AOF | `data` only, **no published port**; named `redis_data` volume |

API and worker need outbound HTTPS for OIDC discovery/token/JWKS access and, only when enabled, the approved model endpoint. Data services have no Internet-facing port or external-route network. A private Docker network is not transport encryption; require verified TLS when substituting remote database/Redis services.

Python 3.12 runs as UID/GID `10001:10001`, without Linux capabilities, with `no-new-privileges`. The only writable application scratch area is a bounded `/run/neuraldesk` tmpfs; Gunicorn worker scratch files use it. Gateway/Redis also run non-root, with read-only root filesystems and narrowly scoped writable areas. Business data is never stored in these scratch areas.

`.dockerignore` allowlists application/build inputs. The image deliberately does not copy `.env`, secrets, legacy SQLite/ChromaDB databases, generated runbook files, tests, or the Git directory. No repository-wide runtime bind mount is needed.

### Database ownership and tenant isolation

The PostgreSQL image runs `docker/postgres/init-runtime-role.sh` **only for a new volume**. It creates `neuraldesk_app` with `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`. Passwords/identifiers are passed through quoted psql variables, not embedded as shell-generated SQL. Default privileges from `POSTGRES_USER` grant runtime DML on future tables and usage/select on sequences.

The migration-owner connection is used only by `migrate` and controlled administration. Its initial migration creates pgvector and enables **FORCE ROW LEVEL SECURITY** on `tickets`, `runbooks`, and `audit_events`. Tenant transactions set `app.tenant_id` locally. The factory rejects a superuser, `BYPASSRLS` login, or protected-table owner, and requires **all three business tables to exist with RLS enabled and forced**. Migrations must therefore finish before API, worker, worker-healthcheck, or Flask CLI startup; all these factory callers use the restricted runtime connection, not the migration owner.

Approved runbook vectors have a partial HNSW cosine index, declared consistently in the model metadata and migration. This index does not replace tenant/category/approval filtering or establish a measured latency/recall guarantee.

Jobs contain global dispatch metadata, not ticket bodies, so workers can claim across companies. The API still tenant-scopes job reads, including requester ownership. RLS is defense against missing business-query filters, **not** permission to expose the shared database login to customers: trusted application code can set its transaction's tenant context. Host, application, migration credentials, and database administration remain security boundaries.

For an existing/managed database, have its administrator create the equivalent non-owner role and pgvector extension, arrange schema/default privileges **for the actual migration owner**, and grant runtime DML on existing tables/sequences as well. The startup script's default privileges affect future objects, not retroactively existing ones. Never “fix” permission errors by switching `DATABASE_URL` to a superuser.

### Redis password handling

`docker/redis-entrypoint.sh` reads `REDIS_PASSWORD` or its file alternative, validates a long hex password, and writes `requirepass` to a mode-0600 tmpfs configuration. The password is not interpolated into a shell command or passed in Redis command-line arguments. Health probes authenticate through `REDISCLI_AUTH`.

AOF uses `appendfsync everysec`: a crash may lose the most recent Redis updates. Redis is **not the durable job queue**. Redis loss can invalidate sessions and reset rate-limit history; PostgreSQL retains tickets/jobs. `noeviction` avoids silently evicting session/limiter keys, but memory exhaustion will fail requests, so budget memory and alert on usage.

Shared admission limits use an atomic Redis Lua operation for increment/expiry/TTL, implemented directly in `app/rate_limit.py`. They do not depend on a process-local counter or Flask-Limiter.

## Production TLS and forwarded headers

The included gateway speaks HTTP on a loopback-published port. Put an operated TLS reverse proxy on the same trusted host, forwarding the public hostname to `http://127.0.0.1:8000`, or supply an explicitly reviewed equivalent TLS topology. Do not change the host binding to `0.0.0.0` as a substitute for TLS.

1. Set `APP_ENV=production`, `PUBLIC_URL=https://support.example.com`, and HTTPS issuer/model URLs.
2. Register **exactly** `https://support.example.com/auth/callback` at the IdP.
3. Configure certificates, renewal, secure TLS policies, HTTP-to-HTTPS redirects, and hostname preservation at the TLS edge.
4. Keep the API unpublished and `TRUSTED_PROXY_HOPS=1` behind the supplied gateway.
5. Ensure the TLS edge also omits/redacts OAuth callback query strings and never logs Authorization headers, cookies, or request bodies.

The gateway **replaces**, rather than appends, `X-Forwarded-For`, `X-Real-IP`, `X-Forwarded-Proto`, and `X-Forwarded-Host`, and strips the other supported forwarded metadata. Its forwarded scheme is derived from the operator-pinned `PUBLIC_URL`, never from a client's `X-Forwarded-Proto`. This does not itself provide TLS: only the trusted TLS edge must be publicly reachable. Callback origin and trusted host come from `PUBLIC_URL`, not these headers.

A separate TLS edge is the immediate network peer seen by nginx. Until explicitly configured otherwise, IP admission limiting will therefore aggregate users behind that peer. To preserve actual client addresses, configure nginx's real-IP module **only for the exact trusted edge address/CIDR**, and require that edge to overwrite incoming forwarded-for headers. Then nginx's `$remote_addr` can be used for the newly generated header. Do not use `set_real_ip_from 0.0.0.0/0`, blindly trust arbitrary chains, or increase proxy hops to compensate. Untrusted workloads must not attach to the API network.

Gateway and Gunicorn access logs are disabled. The exact OAuth callback location also suppresses nginx upstream error logging to avoid leaking codes on failures. Application request logs use route templates/request IDs, not callback queries or ticket bodies. Maintain equivalent redaction at every added proxy/log collector.

## Start, inspect, and scale

```bash
docker compose config --quiet
docker compose up --build --detach --wait --wait-timeout 180
docker compose ps --all
docker compose logs --tail 100 api worker migrate
curl --fail http://localhost:8000/health/ready
```

Use your actual `PUBLIC_URL` hostname for requests. With a custom hostname but a local diagnostic connection, use `curl -H 'Host: support.example.com' http://127.0.0.1:8000/health/ready`. `docker/healthcheck.py` automatically sends the configured public authority in its `Host` header while connecting to internal `127.0.0.1:8000`. It uses only the standard library and probes the existing server; it does **not** import the factory or initialize an AI client.

Both API and worker depend on a healthy database/Redis and a successfully completed migration. Exit code **0** on the one-shot migration container is expected. An unhealthy container is reported by Docker; `restart: unless-stopped` restarts an **exited** process, not every merely unhealthy process. Alert and provide an operator/orchestrator recovery policy.

Scale processes as separate containers:

```bash
docker compose up --detach --wait --wait-timeout 180 --scale api=2 --scale worker=2
```

There is no `container_name` or per-API host port to prevent scaling. nginx uses `resolver 127.0.0.11`, an upstream `zone`, and `server api:8000 resolve` to refresh replica addresses (10-second DNS validity), rather than resolving the service only at startup. This is supported by the nginx 1.28 image. Replica replacement can still cause in-flight failures; clients should use appropriate retries and an `Idempotency-Key` for intake.

**Do not raise Gunicorn's worker count** without deliberately redesigning metrics aggregation. Prometheus counters/histograms are process-local; one worker keeps each API target coherent. Eight threads permit concurrent I/O but are not a capacity guarantee.

Connection planning uses `DATABASE_POOL_SIZE + 5` as each process's potential SQLAlchemy pool ceiling. At defaults, two APIs plus two workers can budget **40 pooled connections**, plus worker healthcheck processes (roughly one extra connection per simultaneous probe), migration/CLI sessions, monitoring, and PostgreSQL maintenance/reserved connections. Pools are lazy, so this is a ceiling budget rather than a claim about steady-state usage. Keep the combined budget below database limits; size CPU, RAM, Redis connections, provider quotas, and queue latency from measured load.

### Worker shutdown and processing guarantees

Workers claim durable jobs with `FOR UPDATE SKIP LOCKED`; ticket/job creation is one PostgreSQL transaction. Default SDK calls are bounded at 40 seconds, job leases are 180 seconds, and jobs have at most three processing attempts. Lease tokens fence stale completions. This is retryable processing, **not exactly-once external model billing**.

`docker compose stop worker` sends termination and allows up to **five minutes** for the current bounded job to finish. Do not replace graceful shutdown with a forced kill during normal deployment. The configuration requires at least two SDK timeouts plus 30 seconds in the job lease; retest grace/lease/heartbeat behavior when tuning. If a worker crashes, a later worker can reclaim an expired lease without allowing the old completion to overwrite current state.

## Published images and upgrades

The workflow publishes an API/worker/migration image to `ghcr.io/<owner>/<repository>` after trusted `main` pushes pass tests and the build/smoke checks. Tags are `sha-<full-commit>` and `latest`; record and deploy a **reviewed digest** for reproducibility. The current CI runner builds a Linux amd64 image. For another architecture, build/test it explicitly rather than assuming a multi-architecture manifest.

If the package is private, authenticate Docker using an operator credential with package-read permission; do not place registry credentials in the application `.env`. Pin the same image for all three services:

```dotenv
NEURALDESK_IMAGE=ghcr.io/udaykyama/agentic-it-support@sha256:<reviewed-image-digest>
```

For a maintenance-window upgrade:

1. Record the running image digest/schema version and take a verified backup. Review schema compatibility and release changes.
2. Drain incoming traffic at the TLS edge and gracefully stop API/worker processes.
3. Update `NEURALDESK_IMAGE`, pull the new image, and explicitly run the migration.
4. Start the new replicas, verify health/auth/tenant isolation, and restore traffic.

```bash
docker compose stop api worker
docker compose pull api worker migrate
docker compose run --rm --no-deps migrate
docker compose up --detach --no-build --wait --wait-timeout 180 \
  --scale api=2 --scale worker=2
```

Do not rely on a previously exited migration container to apply a new schema. `alembic upgrade head` is safe to rerun; dependent startup also requires migration success. Keep bootstrap/migration credentials away from API and worker. Image rollback alone is safe only if the schema remains backward-compatible; otherwise use a planned restoration/forward-fix procedure, not an automatic destructive Alembic downgrade.

The Python/nginx/pgvector base tags track their selected version lines and Redis is patch-tagged. For a controlled release, record resolved image digests, review patch updates, rebuild, and rerun validation. Pinning an old digest indefinitely is not a security-update policy.

## Backup and restore

PostgreSQL contains tenants, tickets, runbooks/embeddings, audit events, and durable jobs. Back up **the entire database**, not only ticket rows. A PostgreSQL logical dump is a transactionally consistent snapshot, not continuous point-in-time recovery; arrange WAL archiving/PITR or a managed equivalent for your required recovery objective.

The following writes a protected file in the working directory using the migration owner. It does not print a database URL or put a password in arguments:

```bash
mkdir -p backups
chmod 700 backups
backup="backups/neuraldesk-$(date -u +%Y%m%dT%H%M%SZ).dump"
(umask 077; set -C; docker compose exec -T db sh -c \
  'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom --no-password' \
  > "$backup") &&
test -s "$backup"
```

The supplied PostgreSQL image permits owner administration over its local container socket. With a managed/hardened database, use its approved backup client authentication and verified TLS instead. Check the dump command's exit status, keep failed/partial files out of the backup catalog, encrypt backups, and store copies off-host under restricted access. Back up deployment configuration and recovery credentials separately through your secret-management process.

### Restore drill

Run this only on a **fresh, isolated restore host/project or an explicitly approved replacement database**. `--clean` deletes destination objects represented by the dump. Never aim it at a live production database for a rehearsal.

1. Obtain the reviewed image/Compose revision, a usable dump, and new/protected credentials. Preserve the original migration-owner role name, database name, and `neuraldesk_app` role convention.
2. Use a distinct Compose project and fresh named volumes. Start only the data services; their first initialization creates the restricted runtime role. Do not start the application against the empty database.
3. Restore as the migration owner, then run migrations for the intended application version.

```bash
export COMPOSE_PROJECT_NAME=neuraldesk-restore
docker compose up -d --wait db redis
docker compose exec -T db sh -c \
  'exec pg_restore --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" \
    --clean --if-exists --no-owner --exit-on-error --single-transaction' \
  < backups/selected-backup.dump
docker compose run --rm --no-deps migrate
# Keep workers stopped until the restored queue and provider access are approved.
docker compose up -d --no-build --wait --wait-timeout 180 api gateway
```

4. Verify readiness, runtime role restrictions/forced RLS, two-company isolation, ticket/runbook counts, approved vector lookup, and access/audit behavior. Check restored pending/running jobs: expired leases can be reclaimed and interrupted model calls can be repeated. Only after approving the queue/provider policy, start workers with `docker compose up -d --no-build --wait worker`.
5. Measure the actual recovery time and recoverable data interval. A nonempty backup file without a successful restore test is not evidence of recoverability.

Use an isolated host for the complete drill so its loopback gateway port does not conflict with the live stack. Do not expose restored personal data or contact real providers unintentionally; keep `AI_ENABLED=false` and test IdP/provider configuration until explicitly approved. Disabling AI does not pause dispatch: a worker processing a restored AI job while AI is disabled marks that job failed. Keep workers stopped to preserve the queue until the recovery policy and AI configuration are ready.

Redis's AOF directory is persisted separately. A safe filesystem copy requires quiescing it or a consistent volume snapshot that includes the whole AOF layout; do not copy individual live AOF files mid-rewrite. Define whether restoring short-lived sessions/limits is appropriate: often invalidating sessions and forcing fresh sign-in is safer. Never treat Redis backups as a replacement for PostgreSQL backups.

`docker compose down` preserves named volumes. **`docker compose down --volumes` destroys them** and is not a routine restart or upgrade command.

## Credential rotation

Plan a maintenance window or a deliberately tested dual-credential transition. Updating `.env`/a secret file and restarting **does not rotate a PostgreSQL role password** on an existing volume.

1. Gracefully stop/drain application processes as appropriate.
2. Open an administrative psql session without putting passwords in arguments:

   ```bash
   docker compose exec db sh -c \
     'exec psql --no-psqlrc --username="$POSTGRES_USER" --dbname="$POSTGRES_DB"'
   ```

   Use psql's `\password neuraldesk_app` prompt to change the runtime password; rotate the migration owner with `\password <owner-name>` if needed. Do not paste literal-password `ALTER ROLE` statements into recorded terminals or SQL logging systems.

3. Update `APP_DB_PASSWORD` and the full `DATABASE_URL` secret (or its interpolated source), and update `POSTGRES_PASSWORD`/`MIGRATION_DATABASE_URL` for owner rotation. Use fresh independent hex values and keep file/direct modes mutually exclusive.
4. For Redis, update `REDIS_PASSWORD` and `REDIS_URL` together, then recreate Redis and application processes. The generated Redis config is rebuilt on startup; the AOF volume remains.
5. Rotate OIDC/model credentials through their providers, update the corresponding mounted secret, and recreate both API/worker. Rotate `METRICS_TOKEN` together with scrape credentials.
6. For `SECRET_KEY` rotation, recreate **all API replicas** with the new key: the factory derives a new Redis session namespace, so old browser sessions are no longer accepted by those replicas. Old replicas retain their prior configuration until recreated. Under a suspected compromise, also handle the old Redis session entries through a reviewed invalidation/retention operation; avoid indiscriminate Redis flushing. Key rotation does not revoke already issued bearer tokens or log users out of the central IdP.
7. Verify sign-in, two-company access, readiness, worker heartbeat, and metrics; revoke old provider/registry credentials and remove superseded secret files.

Tenant access can be blocked immediately with `flask --app api:create_app tenants disable --id company-a`. IdP role changes and previously issued bearer tokens have their own expiry/revocation behavior; define a full incident response procedure rather than relying on a restart.

## Legacy import

Retained SQLite/runbook files are **manual import sources only**. They are not a startup dependency, an image layer, a live bind mount, or a second source of truth. ChromaDB files/old embeddings are not reused.

Provision the destination company and back up PostgreSQL first. With an authorized, protected `tickets.db` and `data/runbooks` snapshot available, mount them **read-only into one disposable import container**:

```bash
test -f tickets.db && test -d data/runbooks
docker compose run --rm --no-deps \
  --volume "$PWD/tickets.db:/import/tickets.db:ro" \
  --volume "$PWD/data/runbooks:/import/runbooks:ro" \
  api flask --app api:create_app import-legacy \
    --tenant company-a --database /import/tickets.db --runbooks /import/runbooks
```

Ensure the non-root application UID can read the source files without making sensitive archives broadly readable. Do not mount the whole repository or change the normal image to include data.

The CLI opens SQLite read-only, preserves source files and ticket IDs, validates records, imports transactionally, and skips matching previously imported records. Conflicting content/schema/values abort the import. Imported tickets use `reason="legacy_unverified"`: old resolution text becomes an **unverified `recommendation`** with status `recommended`; without that text the status is `escalated`. Actual `resolution` remains null, and unknown subcategories are not invented. Legacy imports alone therefore do not necessarily populate recurrence groups.

Imported runbook Markdown is preserved in `steps`, with status **draft** and no reused embeddings. An administrator must review and request asynchronous approval/re-embedding; oversized/invalid runbooks are rejected rather than silently truncated. Keep the source snapshot for a controlled retention period and reconcile counts/audit results before removing it.

## Health, metrics, and troubleshooting

| Check | Meaning |
|---|---|
| `/health/live` | The HTTP application is responding; no model calls |
| `/health/ready` | PostgreSQL and Redis reachable; no IdP/model quota consumption |
| `python -m app.worker --healthcheck` | Creates the app, validates the non-owner PostgreSQL runtime role/forced RLS, then checks the Redis heartbeat for this container's `socket.gethostname()` |
| `/metrics` with `Authorization: Bearer <METRICS_TOKEN>` | API request/latency metrics plus queue counts; separate monitoring credential |
| `/gateway-health` | nginx itself is responding, not an application readiness guarantee |

Probe configuration is valid even when AI is off and no API key is set. Unlike the standalone HTTP-only API probe, the worker healthcheck loads the full application configuration and checks the database role before Redis; it needs the same non-owner database connection and required configuration as the worker. It makes no model inference request. Worker health is per container, not a shared “some worker is alive” flag. Readiness does not guarantee the IdP/provider is currently healthy; use separate controlled monitoring for those dependencies.

Scrape **each API replica directly on a trusted network**, with the required public `Host` header and metrics bearer, using protected service discovery/target management. Scraping the load-balanced gateway alone mixes process-local counters and is not correct per-replica aggregation. Avoid granting a metrics collector unrestricted Docker-daemon access merely to obtain targets.

Useful initial checks:

```bash
docker compose ps --all
docker compose logs --tail 100 migrate api worker
docker compose exec gateway nginx -c /run/nginx/nginx.conf -t
docker compose exec api python /app/docker/healthcheck.py
```

- **Startup configuration error:** check all placeholders, direct/file conflicts, URL schemes, secret lengths, and lease/timeout relationship. AI-off still requires real OIDC configuration.
- **Migration/role failure:** verify owner connection and first-volume initialization. Existing volumes do not rerun bootstrap scripts. Inspect/fix privileges as an owner; do not disable RLS.
- **Invalid host/callback:** use the exact `PUBLIC_URL` hostname and registered callback. `localhost` and `127.0.0.1` are not interchangeable trusted hosts.
- **Sign-in refused:** verify issuer/audience, flat company/roles in both token types, company provisioning/enablement, and admin-controlled mapping.
- **502 after scaling:** check API health and nginx Docker DNS resolution/network attachment. Allow DNS refresh; do not replace dynamic resolution with stale IPs.
- **Jobs remain pending:** inspect worker health, database claims/leases, AI opt-in/provider timeout, and failed-job error codes. Do not “complete” jobs or mark tickets resolved by editing rows.
- **Insight list empty:** check the company/time window/minimum count and categorized tickets. Manual unclassified intake alone does not create recurrence groups.

Alert on ready/unhealthy state, terminal job failures, queue age, API error/latency trends, database connection/disk/WAL usage, Redis memory/persistence, certificate expiry, and provider throttling. Logs/audit entries are operational records, not a tamper-proof compliance ledger; export/protect them according to your requirements.

## Validation and CI

Local tests use the existing virtual environment and standard-library runner (create the Python 3.12 `.venv` as shown in the README if needed):

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

PostgreSQL integration coverage is enabled only when **both** `TEST_DATABASE_URL` and `TEST_REDIS_URL` are set. Its database name must end in `_test`, be isolated from production, and already have migrations and the restricted non-owner runtime role. The test Redis database must also be disposable/isolated. Never supply a production endpoint to tests.

The [CI workflow](../.github/workflows/ci.yaml) is an executable reference for provisioning test infrastructure: start pgvector PostgreSQL/Redis service containers, run the same runtime-role bootstrap script, migrate as `postgres`, then use **`neuraldesk_app`** for `TEST_DATABASE_URL`. It also builds the non-root image, verifies imports without startup secrets, validates Compose, and exercises real container health, gateway syntax, and replica scale-up/down. The isolated unit-test Redis service is runner-only; the subsequent Compose smoke test verifies the authenticated Redis deployment.

Pull-request jobs have read-only repository permission and do not publish. Only a successful `push` to trusted `main` runs the GHCR job with `contents: read` and `packages: write`, authenticating through `GITHUB_TOKEN`. There are no cloud credentials or automatic cloud deployment steps. Configure branch protection, trusted-runner policies, dependency/image scanning, registry retention, and release approvals according to your organization.

If Docker is unavailable on a development machine, shell/YAML/static checks and unit tests are useful but do **not** prove container behavior. Run the Compose smoke workflow on a Docker-capable runner before deployment; do not claim a successful local image/Compose test unless it actually ran.

## Remaining production responsibilities

- Operated TLS termination, DNS, firewall/network trust, certificate renewal, and a tested proxy topology.
- HA PostgreSQL/Redis or explicit acceptance of single-host downtime; managed failover, TLS, and connection budgets where appropriate.
- Secret manager/KMS, credential rotation, host/container access controls, dependency/image patching, and reviewed releases.
- IdP provisioning, MFA/access policies, administrator-controlled tenant/role mapping, tenant offboarding, and revocation procedures.
- Privacy/security approval before sending ticket, resolution, runbook, or embedding inputs to a model provider; contractual data residency/retention/training controls and spending limits.
- Encrypted off-host backups, restoration/PITR drills, measured recovery objectives, and protected audit/log export.
- Explicit retention/deletion policy covering database rows, embeddings, job/audit metadata, exports, and backups. No automatic business-data retention scheduler is supplied.
- Representative tenant-aware load/failure tests, abuse testing, observability/alerts, on-call runbooks, and measured SLOs. Replica counts alone are not evidence of production capacity.
