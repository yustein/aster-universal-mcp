-- Runs only for a fresh empty PostgreSQL volume, using the bootstrap role.
-- Existing volumes require this explicit operator migration before enrichment.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
