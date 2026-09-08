#!/bin/sh
set -eu

if [ -n "${REDIS_PASSWORD:-}" ] && [ -n "${REDIS_PASSWORD_FILE:-}" ]; then
    printf '%s\n' 'Set REDIS_PASSWORD or REDIS_PASSWORD_FILE, not both' >&2
    exit 1
fi
if [ -n "${REDIS_PASSWORD_FILE:-}" ]; then
    redis_password="$(cat -- "$REDIS_PASSWORD_FILE")"
else
    redis_password="${REDIS_PASSWORD:-}"
fi

# Restrict the bootstrap password to URL-safe hex; no shell or Redis-config quoting.
if [ "${#redis_password}" -lt 32 ]; then
    printf '%s\n' 'Redis requires a random hex password of at least 32 characters' >&2
    exit 1
fi
case "$redis_password" in
    *[!0-9a-fA-F]*)
        printf '%s\n' 'REDIS_PASSWORD must be hexadecimal; generate it with openssl rand -hex 32' >&2
        exit 1
        ;;
esac

if [ "${1:-}" = "--healthcheck" ]; then
    REDISCLI_AUTH="$redis_password"
    export REDISCLI_AUTH
    [ "$(redis-cli --no-auth-warning --raw -h 127.0.0.1 ping)" = "PONG" ]
    exit
fi

umask 077
printf 'requirepass %s\n' "$redis_password" > /run/redis/auth.conf
unset redis_password REDIS_PASSWORD REDIS_PASSWORD_FILE
exec redis-server /usr/local/etc/redis/redis.conf
