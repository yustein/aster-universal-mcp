#!/bin/sh
set -eu

# PostgreSQL's entrypoint runs this only for a new empty database volume.
# psql quoted variables preserve password bytes without SQL interpolation.
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set ON_ERROR_STOP=1 \
  --set ingest_password="$ARCHIVE_INGEST_PASSWORD" \
  --set read_password="$ARCHIVE_READ_PASSWORD" <<'SQL'
BEGIN;
CREATE ROLE aster_archive_ingest LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  PASSWORD :'ingest_password';
CREATE ROLE aster_archive_read LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
  PASSWORD :'read_password';
ALTER DATABASE aster_archive OWNER TO aster_archive_ingest;
REVOKE ALL ON DATABASE aster_archive FROM PUBLIC;
GRANT CONNECT ON DATABASE aster_archive TO aster_archive_ingest, aster_archive_read;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA IF NOT EXISTS aster_archive AUTHORIZATION aster_archive_ingest;
GRANT USAGE ON SCHEMA aster_archive TO aster_archive_read;
ALTER DEFAULT PRIVILEGES FOR ROLE aster_archive_ingest IN SCHEMA aster_archive
  GRANT SELECT ON TABLES TO aster_archive_read;
ALTER ROLE aster_archive_read SET default_transaction_read_only = on;
COMMIT;
SQL
