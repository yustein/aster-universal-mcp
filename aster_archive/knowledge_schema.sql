-- Additive derivations only. The canonical v1 schema/checksum stays unchanged.
CREATE TABLE aster_archive.knowledge_jobs (
    revision_id text NOT NULL REFERENCES aster_archive.conversation_revisions,
    pipeline_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','complete','failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    started_at timestamptz,
    finished_at timestamptz,
    error_code text,
    PRIMARY KEY (revision_id,pipeline_key)
);
CREATE TABLE aster_archive.knowledge_records (
    record_id text PRIMARY KEY,
    revision_id text NOT NULL REFERENCES aster_archive.conversation_revisions,
    pipeline_key text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('summary','decision_candidate','cognee_summary')),
    title text NOT NULL,
    text text NOT NULL,
    metadata jsonb NOT NULL,
    citations jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    lexemes_simple tsvector GENERATED ALWAYS AS
        (to_tsvector('pg_catalog.simple'::regconfig,text)) STORED,
    lexemes_english tsvector GENERATED ALWAYS AS
        (to_tsvector('pg_catalog.english'::regconfig,text)) STORED,
    lexemes_turkish tsvector GENERATED ALWAYS AS
        (to_tsvector('pg_catalog.turkish'::regconfig,text)) STORED
);
CREATE INDEX knowledge_records_revision ON aster_archive.knowledge_records(revision_id,pipeline_key,kind);
CREATE INDEX knowledge_records_simple ON aster_archive.knowledge_records USING gin(lexemes_simple);
CREATE INDEX knowledge_records_english ON aster_archive.knowledge_records USING gin(lexemes_english);
CREATE INDEX knowledge_records_turkish ON aster_archive.knowledge_records USING gin(lexemes_turkish);
CREATE TABLE aster_archive.cognee_jobs (
    passage_id text NOT NULL REFERENCES aster_archive.passages,
    pipeline_key text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','complete','failed')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    started_at timestamptz,
    finished_at timestamptz,
    error_code text,
    receipt jsonb,
    PRIMARY KEY(passage_id,pipeline_key)
);
