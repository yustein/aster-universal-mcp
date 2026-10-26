"""Bounded, resumable local Cognee enrichment, isolated from every cloud dataset.

The parent owns PostgreSQL receipts. A separate CPU-only interpreter processes
one exact passage at a time without database credentials or outbound networking.
The persisted graph is isolated per immutable conversation revision. No Cognee
endpoint or raw graph is exposed through the archive's authenticated reader.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from psycopg.types.json import Jsonb

from .access import ArchiveScope
from .knowledge import KnowledgeStore, _citation
from .store import PostgresArchive, _identity, _scope_sql, stable_id
from .storage import minimum_free_bytes, require_storage_reserve

COGNEE_VERSION = '1.6.3'
GLINER_MODEL = 'fastino/gliner2.5-base-v1'
GLINER_REVISION = '54785d51df8d86a0d8f2eb212fec15871aad2c4d'
EMBEDDING_MODEL = 'BAAI/bge-small-en-v1.5'
PIPELINE_KEY = 'cognee-1.6.3-gliner2.1.0-base-54785d51-bge-small-en-archive-schema-v1-pilot'
MAX_ATTEMPTS = 3
SAMPLE_LIMIT = 5
DEFAULT_ROOT = Path('/var/lib/aster-archive/cognee')
DEFAULT_MODEL_ROOT = Path('/var/lib/aster-archive-models/cognee')


class CogneeUnavailable(RuntimeError):
    pass


def verified_model_manifest() -> dict:
    """The release-pinned hashes ship in the wheel, independent of a checkout."""
    return json.loads(resources.files('aster_archive').joinpath('cognee-model-manifest.json').read_text(encoding='utf-8'))


@dataclass(frozen=True)
class CogneeConfig:
    python: Path
    root: Path
    model_root: Path
    timeout_seconds: int = 900
    memory_bytes: int = 6 * 1024**3
    cpu_threads: int = 2

    def validate(self, *, models=True):
        if not self.python.is_absolute() or not self.python.is_file():
            raise ValueError('absolute isolated Cognee interpreter required')
        if not self.root.is_absolute() or not self.model_root.is_absolute():
            raise ValueError('absolute dedicated Cognee storage roots required')
        if (type(self.timeout_seconds) is not int or not 10 <= self.timeout_seconds <= 3600
                or type(self.memory_bytes) is not int or not 2*1024**3 <= self.memory_bytes <= 12*1024**3
                or type(self.cpu_threads) is not int or not 1 <= self.cpu_threads <= 4):
            raise ValueError('invalid Cognee resource budget')
        if models:
            manifest = self.model_root / 'manifest.json'
            if not manifest.is_file():
                raise CogneeUnavailable('COGNEE_MODELS_NOT_PREPARED')
            data = json.loads(manifest.read_text())
            if data != verified_model_manifest():
                raise CogneeUnavailable('COGNEE_MODEL_MANIFEST_MISMATCH')

    @classmethod
    def from_env(cls):
        return cls(Path(os.environ.get('ASTER_ARCHIVE_COGNEE_PYTHON','')),
                   Path(os.environ.get('ASTER_ARCHIVE_COGNEE_ROOT',str(DEFAULT_ROOT))),
                   Path(os.environ.get('ASTER_ARCHIVE_COGNEE_MODEL_ROOT',str(DEFAULT_MODEL_ROOT))))


def child_environment(config: CogneeConfig, root: Path, *, offline=True) -> dict:
    """An allowlist prevents cloud keys, .env discovery and model API inheritance."""
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    env_file = root / 'local-only.env'
    if not env_file.exists():
        env_file.write_text('# Dedicated local archive runtime: deliberately no credentials.\n')
        env_file.chmod(0o600)
    if env_file.is_symlink() or env_file.read_text() != '# Dedicated local archive runtime: deliberately no credentials.\n':
        raise ValueError('Cognee local environment file was changed')
    env = {key: os.environ[key] for key in ('PATH','LANG','LC_ALL','SYSTEMROOT') if key in os.environ}
    env.update({
        'PYTHONUNBUFFERED':'1','PYTHONDONTWRITEBYTECODE':'1',
        'COGNEE_ENV_FILE':str(env_file),'DATA_ROOT_DIRECTORY':str(root / 'data'),
        'SYSTEM_ROOT_DIRECTORY':str(root / 'system'),'DB_PROVIDER':'sqlite',
        'DB_NAME':'aster_archive_cognee','GRAPH_DATABASE_PROVIDER':'ladybug',
        'VECTOR_DB_PROVIDER':'lancedb','ENABLE_BACKEND_ACCESS_CONTROL':'true',
        'GRAPH_DATABASE_SUBPROCESS_ENABLED':'false','VECTOR_DB_SUBPROCESS_ENABLED':'false',
        'TELEMETRY_DISABLED':'1','HF_HUB_DISABLE_TELEMETRY':'1','ANONYMIZED_TELEMETRY':'false',
        'LLM_API_KEY':'','OPENAI_API_KEY':'','GRAPH_EXTRACTOR':'gliner_demo',
        'SUMMARY_METHOD':'from_extraction','GLINER_AUTO_INSTALL':'false',
        'GLINER_INFERENCE_THREADS':'1','IMPROVE_AUTO_ENABLED':'false','AUTO_FEEDBACK':'false',
        'ALLOW_HTTP_REQUESTS':'false','CONTRADICTION_DETECTION':'false',
        'EMBEDDING_PROVIDER':'fastembed','EMBEDDING_MODEL':EMBEDDING_MODEL,
        'EMBEDDING_DIMENSIONS':'384','EMBEDDING_MAX_COMPLETION_TOKENS':'256',
        'EMBEDDING_BATCH_SIZE':'1','FASTEMBED_CACHE_PATH':str(config.model_root / 'fastembed'),
        'HF_HOME':str(config.model_root / 'huggingface'),'TOKENIZERS_PARALLELISM':'false',
        'OMP_NUM_THREADS':str(config.cpu_threads),'MKL_NUM_THREADS':str(config.cpu_threads),
        'OPENBLAS_NUM_THREADS':str(config.cpu_threads),'RAYON_NUM_THREADS':str(config.cpu_threads),
        'KUZU_NUM_THREADS':str(config.cpu_threads),'KUZU_BUFFER_POOL_SIZE':str(256*1024**2),
        'KUZU_MAX_DB_SIZE':str(2*1024**3),'LOG_LEVEL':'CRITICAL','LOG_LEVEL_FILE':'CRITICAL',
        'ASTER_COGNEE_PARENT_PID':str(os.getpid()),'ASTER_COGNEE_MODEL_ROOT':str(config.model_root),
        'ASTER_COGNEE_MEMORY_BYTES':str(config.memory_bytes),
        'ASTER_COGNEE_CPU_THREADS':str(config.cpu_threads),
    })
    if not offline:
        # Download-only setup may need an operator-provided corporate proxy.
        # These transport settings never enter historical-data processing.
        for key in ('HTTPS_PROXY','HTTP_PROXY','NO_PROXY','SSL_CERT_FILE','REQUESTS_CA_BUNDLE','https_proxy','http_proxy','no_proxy'):
            if key in os.environ:
                env[key] = os.environ[key]
    if offline:
        env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',ASTER_COGNEE_OFFLINE='1')
    return env


def _run_child(config: CogneeConfig, payload: dict, root: Path, *, prepare=False) -> dict:
    config.validate(models=not prepare)
    env = child_environment(config,root,offline=not prepare)
    script = Path(__file__).with_name('cognee_runner.py')
    peak_rss = 0
    # Inputs and outputs are private temporary files; native/Python libraries
    # cannot inject chat content into operator logs or corrupt the JSON channel.
    with tempfile.TemporaryDirectory(prefix='job-',dir=root) as directory:
        work = Path(directory)
        request = work / 'request.json'; response = work / 'response.json'
        # The isolated child cannot import this package from its own venv.
        # Pass the trusted packaged manifest through its private input file;
        # the cache's self-reported hashes are never their own trust anchor.
        request.write_text(json.dumps(payload | {'verified_model_manifest':verified_model_manifest()},ensure_ascii=False))
        request.chmod(0o600)
        process = subprocess.Popen([str(config.python),'-I',str(script),str(request),str(response)],
            cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            start_new_session=True)
        try:
            deadline = time.monotonic() + config.timeout_seconds
            while True:
                try:
                    code = process.wait(timeout=min(1, max(0.01, deadline-time.monotonic())))
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        raise CogneeUnavailable('COGNEE_TIMEOUT') from None
                    status = Path('/proc') / str(process.pid) / 'status'
                    if status.is_file():
                        for line in status.read_text().splitlines():
                            if line.startswith('VmRSS:'):
                                peak_rss = max(peak_rss,int(line.split()[1])*1024)
                                if peak_rss > config.memory_bytes:
                                    raise CogneeUnavailable('COGNEE_MEMORY_BUDGET')
        except BaseException as error:
            try:
                os.killpg(process.pid,signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid,signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CogneeUnavailable('COGNEE_CHILD_INTERRUPTED') from None
        if not response.is_file() or response.stat().st_size > 128_000:
            raise CogneeUnavailable('COGNEE_CHILD_FAILED')
        data = json.loads(response.read_text())
        if code != 0:
            error = data.get('error_code','unknown') if isinstance(data,dict) else 'unknown'
            if not isinstance(error,str) or not error.isidentifier() or len(error)>80:
                error = 'unknown'
            raise CogneeUnavailable('COGNEE_CHILD_FAILED:' + error)
        if not isinstance(data,dict) or data.get('status') != 'complete':
            raise CogneeUnavailable('COGNEE_RESULT_INVALID')
        previous_peak = data.get('peak_rss_bytes',0)
        data['peak_rss_bytes'] = max(peak_rss,previous_peak if type(previous_peak) is int else 0)
        return data


def prepare_models(config: CogneeConfig) -> dict:
    """Explicit download-only setup; no historical content is ever passed here."""
    config.validate(models=False)
    config.model_root.mkdir(parents=True,exist_ok=True,mode=0o700)
    require_storage_reserve(config.model_root,additional_bytes=2*1024**3)
    return _run_child(config,{'operation':'prepare'},config.root / 'preparation',prepare=True)


def process_passage(config: CogneeConfig, row: dict) -> dict:
    scope_root = config.root / 'revisions' / stable_id(row['owner_id'],row['source_id'],row['revision_id'],PIPELINE_KEY)
    data = _run_child(config,{'operation':'derive','passage_id':row['passage_id'],
        'text':row['text'],'role':row['role']},scope_root)
    summaries = data.get('summaries')
    if (not isinstance(summaries,list) or len(summaries)>16
            or any(not isinstance(s,str) or len(s)>8000 for s in summaries)):
        raise CogneeUnavailable('COGNEE_SUMMARY_INVALID')
    # Preserve measured runtime/RSS with the completed receipt before the DB
    # acknowledgement, so a replay keeps the original processing evidence.
    receipt = scope_root / 'receipts' / (row['passage_id']+'.json')
    temporary = receipt.with_suffix('.parent.tmp')
    with temporary.open('w') as handle:
        json.dump(data,handle,ensure_ascii=False)
        handle.flush(); os.fsync(handle.fileno())
    temporary.chmod(0o600); os.replace(temporary,receipt)
    directory_fd = os.open(receipt.parent,os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return data


def seed_jobs(conn, owner_id, source_id, *, backlog=False, limit=SAMPLE_LIMIT):
    """Sample at most five role-balanced passages before explicit backlog enable."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid Cognee seed limit')
    if not backlog:
        count = conn.execute('''SELECT count(*) AS n FROM aster_archive.cognee_jobs j
            JOIN aster_archive.passages p USING(passage_id)
            JOIN aster_archive.conversation_revisions r USING(revision_id)
            WHERE r.owner_id=%s AND r.source_id=%s AND j.pipeline_key=%s''',(owner_id,source_id,PIPELINE_KEY)).fetchone()['n']
        limit = min(limit,SAMPLE_LIMIT-count)
        if limit<=0:
            return 0
    rows = conn.execute('''WITH candidates AS (
        SELECT p.passage_id,r.source_updated,n.role,
          row_number() OVER (PARTITION BY r.revision_id,n.role ORDER BY p.passage_id) AS position
        FROM aster_archive.passages p JOIN aster_archive.nodes n USING(revision_id,node_id)
        JOIN aster_archive.conversation_revisions r USING(revision_id)
        JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
        WHERE c.owner_id=%s AND c.source_id=%s AND c.tombstoned_at IS NULL
          AND c.current_revision=r.revision_id AND n.role IN ('user','assistant')
          AND NOT EXISTS(SELECT 1 FROM aster_archive.cognee_jobs j
              WHERE j.passage_id=p.passage_id AND j.pipeline_key=%s))
        INSERT INTO aster_archive.cognee_jobs(passage_id,pipeline_key)
        SELECT passage_id,%s FROM candidates ORDER BY position,source_updated DESC NULLS LAST,role,passage_id
        LIMIT %s ON CONFLICT DO NOTHING RETURNING passage_id''',
        (owner_id,source_id,PIPELINE_KEY,PIPELINE_KEY,limit)).fetchall()
    return len(rows)


def process_pending(store: PostgresArchive, owner_id: str, source_id: str,
                    archive_root: Path, config: CogneeConfig, limit=2, *, backlog=False, processor=None) -> dict:
    _identity(owner_id); _identity(source_id)
    if type(limit) is not int or not 1<=limit<=20:
        raise ValueError('invalid Cognee batch limit')
    minimum_free_bytes(); config.validate()
    processor = processor or process_passage
    knowledge = KnowledgeStore(store)
    with store.import_lock(owner_id,'cognee:' + stable_id(source_id,PIPELINE_KEY)) as conn:
        if backlog:
            counts = conn.execute('''SELECT j.status,count(*) AS n FROM aster_archive.cognee_jobs j
                JOIN aster_archive.passages p USING(passage_id)
                JOIN aster_archive.conversation_revisions r USING(revision_id)
                WHERE r.owner_id=%s AND r.source_id=%s AND j.pipeline_key=%s GROUP BY j.status''',
                (owner_id,source_id,PIPELINE_KEY)).fetchall()
            values = {r['status']:r['n'] for r in counts}
            if values.get('complete',0)<5 or values.get('failed',0):
                raise CogneeUnavailable('COGNEE_SAMPLE_REQUIRED: complete at least five sample passages and resolve failed jobs first')
        seed_jobs(conn,owner_id,source_id,backlog=backlog,limit=max(limit,SAMPLE_LIMIT))
        conn.execute('''UPDATE aster_archive.cognee_jobs j SET status='failed',error_code='attempts_exhausted'
            FROM aster_archive.passages p JOIN aster_archive.conversation_revisions r USING(revision_id)
            WHERE j.passage_id=p.passage_id AND r.owner_id=%s AND r.source_id=%s
            AND j.pipeline_key=%s AND j.status='running' AND j.attempts>=%s''',(owner_id,source_id,PIPELINE_KEY,MAX_ATTEMPTS))
        jobs = conn.execute('''SELECT p.*,n.role,r.owner_id,r.source_id,r.title,r.conversation_id
            FROM aster_archive.cognee_jobs j JOIN aster_archive.passages p USING(passage_id)
            JOIN aster_archive.nodes n USING(revision_id,node_id)
            JOIN aster_archive.conversation_revisions r USING(revision_id)
            JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
            WHERE c.owner_id=%s AND c.source_id=%s AND c.tombstoned_at IS NULL
              AND c.current_revision=r.revision_id AND j.pipeline_key=%s
              AND j.status IN ('pending','running') AND j.attempts<%s
            ORDER BY j.passage_id LIMIT %s''',(owner_id,source_id,PIPELINE_KEY,MAX_ATTEMPTS,limit)).fetchall()
        completed, failed = 0, 0
        for row in jobs:
            require_storage_reserve(Path(archive_root))
            config.root.mkdir(parents=True,exist_ok=True,mode=0o700)
            require_storage_reserve(config.root,additional_bytes=64*1024**2)
            pid = row['passage_id']
            conn.execute('''UPDATE aster_archive.cognee_jobs SET status='running',attempts=attempts+1,started_at=now()
                WHERE passage_id=%s AND pipeline_key=%s''',(pid,PIPELINE_KEY))
            try:
                result = processor(config,row)
                summaries = result['summaries']
                if not summaries:
                    # Successful graph execution with no summary is a measurable
                    # extraction gap, not a fabricated source summary.
                    summaries = []
                records = [{
                    'id':stable_id(PIPELINE_KEY,pid,i),'revision_id':row['revision_id'],
                    'pipeline_key':PIPELINE_KEY,'kind':'cognee_summary',
                    'title':row['title'] or 'Untitled conversation','text':text,
                    'citations':[_citation(row,0,len(row['text']))],
                    'metadata':{'extractor':'Cognee GLiNER demo','pipeline_key':PIPELINE_KEY,
                        'summary_method':'Entities and relation text derived by the local GLiNER model; not an LLM-written narrative.',
                        'verified_decision':False,'role':row['role'],'content_is_historical_evidence':True,
                        'language_coverage':'Experimental English embedding model; not equivalent to the multilingual archive index.',
                        'gliner_model':GLINER_MODEL,'gliner_revision':GLINER_REVISION,
                        'embedding_model':EMBEDDING_MODEL,'model_output_is_unverified':True,
                        'source_scope':'one exact passage dataset in an isolated conversation revision store'},
                } for i,text in enumerate(summaries)]
                receipt = {k:result.get(k) for k in ('graph_nodes','graph_edges','chunks','candidate_edges','kept_edges','elapsed_seconds','peak_rss_bytes','manifest_sha256')}
                receipt.update(summary_count=len(summaries),passage_id=pid,model_language='English embedding pilot')
                with conn.transaction():
                    alive = conn.execute('''SELECT 1 FROM aster_archive.conversations WHERE owner_id=%s
                        AND source_id=%s AND conversation_id=%s AND tombstoned_at IS NULL''',
                        (owner_id,source_id,row['conversation_id'])).fetchone()
                    if alive:
                        knowledge.write_records(conn,records)
                    conn.execute('''UPDATE aster_archive.cognee_jobs SET status='complete',finished_at=now(),error_code=NULL,receipt=%s
                        WHERE passage_id=%s AND pipeline_key=%s''',(Jsonb(receipt),pid,PIPELINE_KEY))
                completed += 1
            except (CogneeUnavailable,ValueError,KeyError):
                conn.execute('''UPDATE aster_archive.cognee_jobs SET status=CASE WHEN attempts>=%s THEN 'failed' ELSE 'pending' END,
                    finished_at=now(),error_code='local_cognee_processing_failed' WHERE passage_id=%s AND pipeline_key=%s''',
                    (MAX_ATTEMPTS,pid,PIPELINE_KEY))
                failed += 1
        return {'completed':completed,'failed':failed,'pipeline_key':PIPELINE_KEY,'mode':'backlog' if backlog else 'bounded_sample'}


def coverage(store,scope):
    where,args = _scope_sql(scope)
    with store.connection(readonly=True) as conn:
        rows = conn.execute(f'''SELECT j.status,j.error_code,count(*) AS passages,
            sum(COALESCE((j.receipt->>'summary_count')::integer,0)) AS summaries
            FROM aster_archive.cognee_jobs j JOIN aster_archive.passages p USING(passage_id)
            JOIN aster_archive.conversation_revisions r USING(revision_id)
            JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
            WHERE {where} AND c.current_revision=r.revision_id AND j.pipeline_key=%s
            GROUP BY j.status,j.error_code''',args+[PIPELINE_KEY]).fetchall()
    return {'pipeline_key':PIPELINE_KEY,'jobs':rows,'mode':'experimental_local_English_embedding_graph',
            'sample_limit':SAMPLE_LIMIT,'graph_is_not_authoritative':True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('prepare','run','status'))
    parser.add_argument('--owner');parser.add_argument('--source');parser.add_argument('--archive-root',type=Path)
    parser.add_argument('--limit',type=int,default=2);parser.add_argument('--enable-backlog',action='store_true')
    args=parser.parse_args(argv)
    config=CogneeConfig.from_env()
    if args.action=='prepare':
        result=prepare_models(config)
    else:
        store=PostgresArchive(os.environ.get('ASTER_ARCHIVE_DATABASE_URL',''))
        _identity(args.owner);_identity(args.source)
        if args.action=='run':
            result=process_pending(store,args.owner,args.source,args.archive_root,config,args.limit,backlog=args.enable_backlog)
        else:
            result=coverage(store,ArchiveScope('local-operator',args.owner,(args.source,),None))
    print(json.dumps(result,ensure_ascii=False,default=str))


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        print(json.dumps({'error':'COGNEE_OPERATION_FAILED'}),file=sys.stderr)
        sys.exit(1)
