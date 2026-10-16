#!/bin/sh
set -eu
# TLS must exist before the upstream entrypoint starts its temporary init server.
mkdir -p /tmp/sky-tls
cp /run/sky/server.crt /tmp/sky-tls/server.crt
cp /run/sky/server.key /tmp/sky-tls/server.key
chown -R postgres:postgres /tmp/sky-tls
chmod 700 /tmp/sky-tls
chmod 600 /tmp/sky-tls/server.key
exec /usr/local/bin/docker-entrypoint.sh "$@"
