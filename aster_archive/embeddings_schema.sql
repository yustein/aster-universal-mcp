-- Requires an operator-installed pgvector extension in public. The ordinary
-- ingest role applies this migration; MCP readers never install extensions.
CREATE TABLE aster_archive.embedding_generations (
    generation_id text PRIMARY KEY CHECK (generation_id ~ '^[a-f0-9]{64}$'),
    descriptor jsonb NOT NULL,
    dimensions integer NOT NULL CHECK (dimensions = 384),
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Deduplicate before inference within an owner. Passage text remains exclusively
-- in the canonical archive. Every read must rejoin its authorized current rows.
CREATE TABLE aster_archive.embedding_jobs (
    generation_id text NOT NULL REFERENCES aster_archive.embedding_generations,
    owner_id text NOT NULL,
    text_sha256 text NOT NULL CHECK (text_sha256 ~ '^[a-f0-9]{64}$'),
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','running','complete','failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token text,
    lease_until timestamptz,
    retry_after timestamptz,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    PRIMARY KEY (generation_id,owner_id,text_sha256)
);
CREATE INDEX embedding_jobs_ready ON aster_archive.embedding_jobs
    (generation_id,owner_id,state,retry_after,created_at);

CREATE TABLE aster_archive.embedding_vectors (
    generation_id text NOT NULL,
    owner_id text NOT NULL,
    text_sha256 text NOT NULL,
    embedding public.vector(384) NOT NULL,
    token_count integer NOT NULL CHECK (token_count >= 0),
    window_count integer NOT NULL CHECK (window_count >= 1),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (generation_id,owner_id,text_sha256),
    FOREIGN KEY (generation_id,owner_id,text_sha256)
        REFERENCES aster_archive.embedding_jobs
);

CREATE INDEX passages_text_sha256 ON aster_archive.passages(text_sha256);
