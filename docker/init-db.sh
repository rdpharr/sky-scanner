#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=explorer_password="$EXPLORER_PASSWORD" <<'SQL'
CREATE ROLE explorer LOGIN PASSWORD :'explorer_password';
GRANT CONNECT ON DATABASE sky_scanner TO explorer;
GRANT USAGE ON SCHEMA public TO explorer;
ALTER DEFAULT PRIVILEGES FOR ROLE sky_scanner IN SCHEMA public GRANT SELECT ON TABLES TO explorer;
SQL
# Internal app connections and LAN exploration both require encrypted password auth.
cat > "$PGDATA/pg_hba.conf" <<EOF
local all all trust
hostssl sky_scanner sky_scanner 172.16.0.0/12 scram-sha-256
hostssl sky_scanner explorer 172.16.0.0/12 scram-sha-256
hostssl sky_scanner explorer ${LAN_SUBNET} scram-sha-256
hostssl sky_scanner explorer 127.0.0.1/32 scram-sha-256
host all all 0.0.0.0/0 reject
host all all ::0/0 reject
EOF
