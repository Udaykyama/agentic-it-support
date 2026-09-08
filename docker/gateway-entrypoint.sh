#!/bin/sh
set -eu

# The external scheme is operator-configured, never accepted from client headers.
case "${PUBLIC_URL:?Set PUBLIC_URL}" in
    https://*) PUBLIC_SCHEME=https ;;
    http://*) PUBLIC_SCHEME=http ;;
    *) printf '%s\n' 'PUBLIC_URL must use http:// or https://' >&2; exit 1 ;;
esac
export PUBLIC_SCHEME

envsubst '$PUBLIC_SCHEME' \
    < /etc/nginx/templates/neuraldesk.conf.template \
    > /run/nginx/nginx.conf
exec nginx -c /run/nginx/nginx.conf -g 'daemon off;'
