#!/usr/bin/env bash
# Activates pgvector in the application database.
#
# Runs once, on an empty data directory, like 01-create-keycloak-db.sh. The
# extension binaries ship with the pgvector/pgvector image; this only turns it
# on for our database. Keycloak's database has no use for it, and enabling an
# extension it does not need would widen its surface for nothing.
#
# Init scripts only run on an empty data directory, so an existing volume
# has to be recreated for this to take effect.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-'SQL'
    CREATE EXTENSION IF NOT EXISTS vector;
SQL

echo "pgvector enabled in database $POSTGRES_DB"
