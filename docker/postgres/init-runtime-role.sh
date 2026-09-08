#!/bin/sh
set -eu

if [ -n "${APP_DB_PASSWORD:-}" ] && [ -n "${APP_DB_PASSWORD_FILE:-}" ]; then
    printf '%s\n' 'Set APP_DB_PASSWORD or APP_DB_PASSWORD_FILE, not both' >&2
    exit 1
fi
if [ -n "${APP_DB_PASSWORD_FILE:-}" ]; then
    APP_DB_PASSWORD="$(cat -- "$APP_DB_PASSWORD_FILE")"
fi
: "${APP_DB_PASSWORD:?Set APP_DB_PASSWORD or APP_DB_PASSWORD_FILE}"
: "${POSTGRES_USER:?Set the migration owner in POSTGRES_USER}"
: "${POSTGRES_DB:?Set POSTGRES_DB}"
export APP_DB_PASSWORD POSTGRES_USER POSTGRES_DB

# psql quotes variables as SQL literals/identifiers, not interpolated SQL or argv.
psql --no-psqlrc --set=ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv runtime_password APP_DB_PASSWORD
\getenv migration_owner POSTGRES_USER
\getenv database_name POSTGRES_DB
BEGIN;
CREATE ROLE neuraldesk_app WITH LOGIN PASSWORD :'runtime_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO neuraldesk_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO neuraldesk_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_owner" IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO neuraldesk_app;
ALTER DEFAULT PRIVILEGES FOR ROLE :"migration_owner" IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO neuraldesk_app;
COMMIT;
SQL
unset APP_DB_PASSWORD
