#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
DEMO_ENV="$ROOT/demo/demo.env"
PROJECT="runbooksignal-demo"

fail() {
    printf 'RunbookSignal demo: %s\n' "$1" >&2
    exit 1
}

compose() (
    unset APP_ENV DATABASE_URL DATABASE_URL_FILE REDIS_URL REDIS_URL_FILE
    unset SECRET_KEY SECRET_KEY_FILE PUBLIC_URL OIDC_ISSUER OIDC_CLIENT_ID
    unset OIDC_CLIENT_SECRET OIDC_CLIENT_SECRET_FILE OIDC_AUDIENCE
    unset OIDC_TENANT_CLAIM OIDC_ROLES_CLAIM OIDC_DISCOVERY_URL DEMO_MODE
    unset METRICS_TOKEN METRICS_TOKEN_FILE AI_ENABLED OPENAI_API_KEY
    unset OPENAI_API_KEY_FILE OPENAI_BASE_URL CLASSIFICATION_MODEL EMBEDDING_MODEL
    unset AI_TIMEOUT_SECONDS CONFIDENCE_THRESHOLD RUNBOOK_SIMILARITY_THRESHOLD
    unset SESSION_LIFETIME_SECONDS JOB_LEASE_SECONDS JOB_MAX_ATTEMPTS
    unset WORKER_POLL_SECONDS RATE_LIMIT_PER_MINUTE AI_RATE_LIMIT_PER_MINUTE
    unset DATABASE_POOL_SIZE LOG_LEVEL TRUSTED_PROXY_HOPS DEMO_STUB_CONTROL_TOKEN
    unset POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD POSTGRES_PASSWORD_FILE
    unset APP_DB_PASSWORD APP_DB_PASSWORD_FILE REDIS_PASSWORD REDIS_PASSWORD_FILE
    unset MIGRATION_DATABASE_URL MIGRATION_DATABASE_URL_FILE NEURALDESK_IMAGE
    unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
    unset COMPOSE_DISABLE_ENV_FILE
    docker compose \
        --project-directory "$ROOT" \
        --project-name "$PROJECT" \
        --env-file "$DEMO_ENV" \
        -f "$ROOT/compose.yaml" \
        -f "$ROOT/demo/compose.demo.yaml" \
        "$@"
)

check_runtime() {
    command -v docker >/dev/null 2>&1 || fail "Docker is required."
    docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."
    docker compose up --help 2>/dev/null | grep -q -- '--wait' ||
        fail "Docker Compose must support 'up --wait'. Update Docker Desktop or Compose v2."
    case "${DOCKER_HOST:-}" in
        ""|unix://*|npipe://*) ;;
        *) fail "Refusing a remote DOCKER_HOST. Use a local Docker engine for this demo." ;;
    esac
    context="$(docker context show 2>/dev/null)" ||
        fail "The active Docker context could not be read."
    endpoint="$(docker context inspect "$context" --format '{{.Endpoints.docker.Host}}' 2>/dev/null)" ||
        fail "The active Docker endpoint could not be read."
    case "$endpoint" in
        unix://*|npipe://*) ;;
        *) fail "Refusing Docker context '$context' because it is not a local socket." ;;
    esac
    docker info >/dev/null 2>&1 || fail "The local Docker engine is not available."
}

start() {
    check_runtime
    compose config --quiet
    compose up --build --detach --wait --wait-timeout 300
    compose exec -T api python /app/demo_seed.py ensure
    printf '\nRunbookSignal synthetic demo is ready:\n'
    printf '  Dashboard: http://localhost:8000\n'
    printf '  Local identity provider: http://localhost:8081\n'
    printf '  Guide: docs/demo.md\n'
    printf '\nNormal start preserves presenter progress. Use %s reset for a clean scenario.\n' "$0"
}

reset() {
    check_runtime
    compose config --quiet
    printf '%s\n' "Stopping demo intake and draining workers before resetting synthetic records..."
    gateway_id="$(compose ps --quiet gateway)"
    if [ -n "$gateway_id" ]; then
        compose stop --timeout 75 gateway
    fi
    worker_id="$(compose ps --quiet worker)"
    if [ -n "$worker_id" ]; then
        compose stop --timeout 300 worker
    fi
    compose up --build --detach --wait --wait-timeout 300 api keycloak model-stub
    if compose exec -T api python /app/demo_seed.py reset; then
        compose up --detach --wait --wait-timeout 120 worker gateway
    else
        fail "Reset failed with intake and workers stopped. Inspect '$0 logs' before retrying."
    fi
    printf '%s\n' "The synthetic scenario was reset. Open http://localhost:8000"
}

case "${1:-start}" in
    start)
        [ "$#" -le 1 ] || fail "start takes no additional arguments."
        start
        ;;
    seed)
        [ "$#" -eq 1 ] || fail "seed takes no additional arguments."
        check_runtime
        compose exec -T api python /app/demo_seed.py ensure
        ;;
    reset)
        [ "$#" -eq 1 ] || fail "reset takes no additional arguments."
        reset
        ;;
    stop)
        [ "$#" -eq 1 ] || fail "stop takes no additional arguments."
        check_runtime
        compose down --remove-orphans
        ;;
    status)
        [ "$#" -eq 1 ] || fail "status takes no additional arguments."
        check_runtime
        compose ps --all
        ;;
    logs)
        [ "$#" -eq 1 ] || fail "logs takes no additional arguments."
        check_runtime
        compose logs --no-color --tail 200
        ;;
    *)
        fail "Use start, seed, reset, stop, status, or logs."
        ;;
esac
