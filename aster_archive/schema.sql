CREATE SCHEMA IF NOT EXISTS aster_archive;

CREATE TABLE IF NOT EXISTS aster_archive.schema_version (
    version integer PRIMARY KEY,
    checksum text NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE aster_archive.sources (
    owner_id text NOT NULL,
    source_id text NOT NULL,
    provider text NOT NULL DEFAULT 'chatgpt',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, source_id)
);

CREATE TABLE aster_archive.imports (
    import_id text PRIMARY KEY,
    owner_id text NOT NULL,
    source_id text NOT NULL,
    archive_sha256 text NOT NULL,
    object_path text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    kind text NOT NULL CHECK (kind IN ('zip', 'json')),
    pipeline_version text NOT NULL,
    inventory jsonb NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','complete','complete_with_errors','failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    finished_at timestamptz,
    error_code text,
    FOREIGN KEY (owner_id, source_id) REFERENCES aster_archive.sources,
    UNIQUE (owner_id, source_id, archive_sha256, pipeline_version)
);

CREATE TABLE aster_archive.conversations (
    owner_id text NOT NULL,
    source_id text NOT NULL,
    conversation_id text NOT NULL,
    project_id text,
    current_revision text,
    current_updated double precision,
    current_ambiguous boolean NOT NULL DEFAULT false,
    tombstoned_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, source_id, conversation_id),
    FOREIGN KEY (owner_id, source_id) REFERENCES aster_archive.sources
);

CREATE TABLE aster_archive.conversation_revisions (
    revision_id text PRIMARY KEY,
    owner_id text NOT NULL,
    source_id text NOT NULL,
    conversation_id text NOT NULL,
    content_hash text NOT NULL,
    title text,
    source_created double precision,
    source_updated double precision,
    current_node text,
    metadata jsonb NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (owner_id, source_id, conversation_id)
        REFERENCES aster_archive.conversations,
    UNIQUE (owner_id, source_id, conversation_id, content_hash)
);

CREATE TABLE aster_archive.nodes (
    revision_id text NOT NULL REFERENCES aster_archive.conversation_revisions,
    node_id text NOT NULL,
    parent_id text,
    children jsonb NOT NULL,
    message_id text,
    role text,
    content_type text,
    content_hash text NOT NULL,
    searchable boolean NOT NULL,
    raw jsonb NOT NULL,
    asset_refs jsonb NOT NULL,
    PRIMARY KEY (revision_id, node_id)
);

CREATE TABLE aster_archive.passages (
    passage_id text PRIMARY KEY,
    revision_id text NOT NULL,
    node_id text NOT NULL,
    part_index integer NOT NULL,
    start_char integer NOT NULL,
    end_char integer NOT NULL,
    text text NOT NULL,
    text_sha256 text NOT NULL,
    lexemes_simple tsvector GENERATED ALWAYS AS
        (to_tsvector('pg_catalog.simple'::regconfig, text)) STORED,
    lexemes_english tsvector GENERATED ALWAYS AS
        (to_tsvector('pg_catalog.english'::regconfig, text)) STORED,
    lexemes_turkish tsvector GENERATED ALWAYS AS
        (to_tsvector('pg_catalog.turkish'::regconfig, text)) STORED,
    FOREIGN KEY (revision_id, node_id) REFERENCES aster_archive.nodes,
    CHECK (end_char >= start_char),
    CHECK (char_length(text) = end_char - start_char),
    UNIQUE (revision_id, node_id, part_index, start_char)
);
CREATE INDEX passages_simple_gin ON aster_archive.passages USING gin (lexemes_simple);
CREATE INDEX passages_english_gin ON aster_archive.passages USING gin (lexemes_english);
CREATE INDEX passages_turkish_gin ON aster_archive.passages USING gin (lexemes_turkish);
CREATE INDEX passages_node ON aster_archive.passages (revision_id, node_id, part_index, start_char);
CREATE INDEX revisions_scope ON aster_archive.conversation_revisions (owner_id, source_id, conversation_id);

CREATE TABLE aster_archive.import_items (
    import_id text NOT NULL REFERENCES aster_archive.imports,
    member_name text NOT NULL,
    ordinal bigint NOT NULL,
    conversation_id text,
    revision_id text REFERENCES aster_archive.conversation_revisions,
    status text NOT NULL CHECK (status IN ('imported','invalid','tombstoned')),
    error_code text,
    PRIMARY KEY (import_id, member_name, ordinal)
);

-- A pending import and its delivery obligation are committed together. Queue
-- delivery may repeat after a crash; import identity and per-item transactions
-- make that replay safe. No exactly-once external-side-effect claim is made.
CREATE TABLE aster_archive.import_outbox (
    import_id text PRIMARY KEY REFERENCES aster_archive.imports,
    dispatched_job_id bigint,
    dispatched_at timestamptz
);

-- Held until a model/version, budget and pipeline are explicitly configured.
-- Original records never depend on derived knowledge being available.
CREATE TABLE aster_archive.derivations (
    revision_id text NOT NULL REFERENCES aster_archive.conversation_revisions,
    kind text NOT NULL CHECK (kind IN ('embedding','knowledge')),
    state text NOT NULL DEFAULT 'held' CHECK (state IN ('held','pending','complete','failed')),
    pipeline_key text,
    PRIMARY KEY (revision_id, kind)
);

CREATE TABLE aster_archive.inbox_receipts (
    owner_id text NOT NULL,
    source_id text NOT NULL,
    path text NOT NULL,
    size_bytes bigint NOT NULL,
    mtime_ns bigint NOT NULL,
    import_id text REFERENCES aster_archive.imports,
    error_code text,
    observed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (owner_id, source_id, path, size_bytes, mtime_ns)
);
