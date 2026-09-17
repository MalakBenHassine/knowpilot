#!/bin/bash
# Runs once, the first time the postgres volume is created.
# Keycloak gets its OWN database and its OWN user inside the same instance:
# one server, two isolated tenants. If the Keycloak user leaks, it must not be
# able to read the application data.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE USER "$KP_KEYCLOAK_DB_USER" WITH PASSWORD '$KP_KEYCLOAK_DB_PASSWORD';
    CREATE DATABASE "$KP_KEYCLOAK_DB" OWNER "$KP_KEYCLOAK_DB_USER";

    -- PostgreSQL grants CONNECT to PUBLIC by default, so ANY user can open
    -- ANY database. Least privilege: revoke it, then grant explicitly.
    REVOKE CONNECT ON DATABASE "$POSTGRES_DB" FROM PUBLIC;
    GRANT CONNECT ON DATABASE "$POSTGRES_DB" TO "$POSTGRES_USER";

    REVOKE CONNECT ON DATABASE "$KP_KEYCLOAK_DB" FROM PUBLIC;
    GRANT CONNECT ON DATABASE "$KP_KEYCLOAK_DB" TO "$KP_KEYCLOAK_DB_USER";
EOSQL

echo "Keycloak database '$KP_KEYCLOAK_DB' created and isolated."
