"""Bounded, resumable local Cognee enrichment, isolated from every cloud dataset.

The parent owns PostgreSQL receipts. A separate interpreter processes exact
passages with CPU or explicitly qualified CUDA inference, without credentials
or outbound networking. A bounded session reuses models within one revision.
The persisted graph is isolated per immutable conversation revision. No Cognee
endpoint or raw graph is exposed through the archive's authenticated reader.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import dataclass
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import tempfile
import time

from psycopg.types.json import Jsonb

from .access import ArchiveScope
from .cognee_storage import (compact_revision, hydrated_revision, revision_lock,
                            block_unquiescent_writer, ARCHIVE_NAME, MARKER_NAME, ROOTS)
from .knowledge import KnowledgeStore, _citation
from .store import PostgresArchive, _identity, _scope_sql, stable_id
from .storage import ArchiveStorageError, minimum_free_bytes, require_storage_reserve

COGNEE_VERSION = '1.6.3'
GLINER_MODEL = 'fastino/gliner2.5-base-v1'
GLINER_REVISION = '54785d51df8d86a0d8f2eb212fec15871aad2c4d'
EMBEDDING_MODEL = 'BAAI/bge-small-en-v1.5'
PIPELINE_KEY = 'cognee-1.6.3-gliner2.1.0-base-54785d51-bge-small-en-archive-schema-v1-pilot'
MAX_ATTEMPTS = 3
SAMPLE_LIMIT = 5
MAX_SESSION_PASSAGES = 100
DEFAULT_ROOT = Path('/var/lib/aster-archive/cognee')
DEFAULT_MODEL_ROOT = Path('/var/lib/aster-archive-models/cognee')


class CogneeUnavailable(RuntimeError):
    pass


class CogneeRuntimeUnavailable(CogneeUnavailable):
    """Shared runtime configuration failed; retrying a passage cannot fix it."""


RUNTIME_FAILURE_CODES=frozenset({
    'CUDA_UNAVAILABLE','GLINER_DEVICE_INTERFACE_UNSUPPORTED',
    'GLINER_DEVICE_MISMATCH','GLINER_PRECISION_MISMATCH',
    'RUNTIME_INITIALIZATION_FAILED','MODEL_VERIFICATION_FAILED',
    'LOCAL_ONLY_CONFIGURATION_FAILED','LOCAL_EMBEDDING_CONFIGURATION_FAILED',
    'INVALID_EXECUTION_DEVICE','CHILD_LAUNCH_FAILED',
})


def _child_failure(error):
    if not isinstance(error,str) or not error.isidentifier() or len(error)>80:
        error='unknown'
    if error in RUNTIME_FAILURE_CODES:
        return CogneeRuntimeUnavailable('COGNEE_RUNTIME_UNAVAILABLE:'+error)
    return CogneeUnavailable('COGNEE_CHILD_FAILED:'+error)


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
    device: str = 'cpu'
    gpu_memory_fraction: float = 0.9
    inference_batch_size: int = 1
    compact_completed: bool = False

    def validate(self, *, models=True):
        if (not self.python.is_absolute() or not self.python.is_file()
                or not os.access(self.python,os.X_OK)):
            raise ValueError('absolute executable isolated Cognee interpreter required')
        if not self.root.is_absolute() or not self.model_root.is_absolute():
            raise ValueError('absolute dedicated Cognee storage roots required')
        if (type(self.timeout_seconds) is not int or not 10 <= self.timeout_seconds <= 3600
                or type(self.memory_bytes) is not int or not 2*1024**3 <= self.memory_bytes <= 12*1024**3
                or type(self.cpu_threads) is not int or not 1 <= self.cpu_threads <= 64
                or self.device not in ('cpu','cuda')
                or type(self.gpu_memory_fraction) not in (int,float)
                or not 0.1 <= self.gpu_memory_fraction <= 0.95
                or type(self.inference_batch_size) is not int or not 1 <= self.inference_batch_size <= 16
                or type(self.compact_completed) is not bool):
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
        compact = os.environ.get('ASTER_ARCHIVE_COGNEE_COMPACT_COMPLETED','0')
        if compact not in ('0','1'):
            raise ValueError('Cognee completed revision compaction must be 0 or 1')
        return cls(Path(os.environ.get('ASTER_ARCHIVE_COGNEE_PYTHON','')),
                   Path(os.environ.get('ASTER_ARCHIVE_COGNEE_ROOT',str(DEFAULT_ROOT))),
                   Path(os.environ.get('ASTER_ARCHIVE_COGNEE_MODEL_ROOT',str(DEFAULT_MODEL_ROOT))),
                   timeout_seconds=int(os.environ.get('ASTER_ARCHIVE_COGNEE_TIMEOUT_SECONDS','900')),
                   memory_bytes=int(os.environ.get('ASTER_ARCHIVE_COGNEE_MEMORY_BYTES',str(6*1024**3))),
                   cpu_threads=int(os.environ.get('ASTER_ARCHIVE_COGNEE_CPU_THREADS','2')),
                   device=os.environ.get('ASTER_ARCHIVE_COGNEE_DEVICE','cpu'),
                   gpu_memory_fraction=float(os.environ.get('ASTER_ARCHIVE_COGNEE_GPU_MEMORY_FRACTION','0.9')),
                   inference_batch_size=int(os.environ.get('ASTER_ARCHIVE_COGNEE_INFERENCE_BATCH_SIZE','1')),
                   compact_completed=compact=='1')


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
        # FastEmbed creates its cache directory even when specific_model_path
        # points at an existing immutable snapshot. Keep library metadata in
        # this owner/source/revision runtime, never in the read-only weights.
        'EMBEDDING_BATCH_SIZE':'1','FASTEMBED_CACHE_PATH':str(root / 'cache' / 'fastembed'),
        'HF_HOME':str(root / 'cache' / 'huggingface'),
        'XDG_CACHE_HOME':str(root / 'cache'),'TOKENIZERS_PARALLELISM':'false',
        'OMP_NUM_THREADS':str(config.cpu_threads),'MKL_NUM_THREADS':str(config.cpu_threads),
        'OPENBLAS_NUM_THREADS':str(config.cpu_threads),'RAYON_NUM_THREADS':str(config.cpu_threads),
        'KUZU_NUM_THREADS':str(config.cpu_threads),'KUZU_BUFFER_POOL_SIZE':str(256*1024**2),
        'KUZU_MAX_DB_SIZE':str(2*1024**3),'LOG_LEVEL':'CRITICAL','LOG_LEVEL_FILE':'CRITICAL',
        'ASTER_COGNEE_PARENT_PID':str(os.getpid()),'ASTER_COGNEE_MODEL_ROOT':str(config.model_root),
        'ASTER_COGNEE_MEMORY_BYTES':str(config.memory_bytes),
        'ASTER_COGNEE_CPU_THREADS':str(config.cpu_threads),
        'ASTER_COGNEE_DEVICE':config.device,
        'ASTER_COGNEE_GPU_MEMORY_FRACTION':str(config.gpu_memory_fraction),
        'ASTER_COGNEE_INFERENCE_BATCH_SIZE':str(config.inference_batch_size),
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


def _quiesce_process_group(process,root):
    """Reap the leader and prove no descendant can still write a revision."""
    try:
        for signum, seconds in ((signal.SIGTERM,5),(signal.SIGKILL,2)):
            try:
                os.killpg(process.pid,signum)
            except ProcessLookupError:
                process.wait(timeout=seconds)
                return
            deadline=time.monotonic()+seconds
            while time.monotonic()<deadline:
                process.poll()  # Reap the leader; killpg(0) then covers descendants.
                try:
                    os.killpg(process.pid,0)
                except ProcessLookupError:
                    process.wait(timeout=max(0.1,deadline-time.monotonic()))
                    return
                time.sleep(0.05)
    except (OSError,subprocess.TimeoutExpired):
        pass
    try:
        block_unquiescent_writer(root,process.pid)
    except Exception:
        # If even the barrier cannot be persisted, exit the supervised worker
        # so its container/service cgroup is torn down; never keep processing.
        raise SystemExit(74) from None
    raise CogneeRuntimeUnavailable('COGNEE_CHILD_GROUP_NOT_QUIESCENT')


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
            _quiesce_process_group(process,root)
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            raise CogneeUnavailable('COGNEE_CHILD_INTERRUPTED') from None
        _quiesce_process_group(process,root)
        if not response.is_file() or response.stat().st_size > 128_000:
            raise CogneeUnavailable('COGNEE_CHILD_FAILED')
        data = json.loads(response.read_text())
        if code != 0:
            error = data.get('error_code','unknown') if isinstance(data,dict) else 'unknown'
            raise _child_failure(error)
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
    config.validate()
    scope_root = config.root / 'revisions' / stable_id(row['owner_id'],row['source_id'],row['revision_id'],PIPELINE_KEY)
    with revision_lock(scope_root):
        cached = _read_completed_receipt(config,row,scope_root)
        if cached is not None:
            return _persist_passage_result(config,row,cached,scope_root)
    with hydrated_revision(scope_root):
        data = _run_child(config,{'operation':'derive','passage_id':row['passage_id'],
            'text':row['text'],'role':row['role']},scope_root)
    return _persist_passage_result(config,row,data,scope_root)


def _read_completed_receipt(config,row,scope_root):
    """Replay an exact completed receipt without extracting a frozen graph."""
    if (not isinstance(row.get('passage_id'),str) or not re.fullmatch('[a-f0-9]{64}',row['passage_id'])
            or row.get('role') not in ('user','assistant')
            or not isinstance(row.get('text'),str) or len(row['text'])>2000):
        raise ValueError('invalid Cognee passage metadata or text budget')
    path=scope_root/'receipts'/(row['passage_id']+'.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_size>=128_000:
        return None
    try:
        data=json.loads(path.read_text())
    except (ValueError,UnicodeError):
        return None
    if (not isinstance(data,dict) or data.get('status')!='complete'
            or data.get('source_sha256')!=hashlib.sha256(row['text'].encode()).hexdigest()
            or data.get('manifest_sha256')!=hashlib.sha256((config.model_root/'manifest.json').read_bytes()).hexdigest()):
        return None
    return data|{'reused_completed_receipt':True}


def _persist_passage_result(config: CogneeConfig, row: dict, data: dict, scope_root: Path) -> dict:
    summaries = data.get('summaries')
    if (not isinstance(summaries,list) or len(summaries)>16
            or any(not isinstance(s,str) or len(s)>8000 for s in summaries)):
        raise CogneeUnavailable('COGNEE_SUMMARY_INVALID')
    if (data.get('status')!='complete'
            or data.get('source_sha256')!=hashlib.sha256(row['text'].encode()).hexdigest()
            or data.get('manifest_sha256')!=hashlib.sha256((config.model_root/'manifest.json').read_bytes()).hexdigest()):
        raise CogneeUnavailable('COGNEE_RESULT_SOURCE_MISMATCH')
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


class ScopedCogneeSession:
    """Reuse one credential-free child for at most 100 same-revision rows.

    Each request remains a separately claimed, timed and acknowledged passage.
    A child failure closes the process; a later request starts a clean child.
    Atomic child receipts survive a process or database-ack interruption.
    """
    def __init__(self, config: CogneeConfig, rows: list[dict]):
        config.validate()
        if not 1 <= len(rows) <= MAX_SESSION_PASSAGES:
            raise ValueError('invalid Cognee session size')
        scopes={(r['owner_id'],r['source_id'],r['revision_id']) for r in rows}
        if len(scopes)!=1 or len({r['passage_id'] for r in rows})!=len(rows):
            raise ValueError('Cognee session requires one immutable revision and unique passages')
        self.config=config
        self.rows={r['passage_id']:(hashlib.sha256(r['text'].encode()).hexdigest(),r['role']) for r in rows}
        self.scope=next(iter(scopes))
        self.root=config.root/'revisions'/stable_id(*self.scope,PIPELINE_KEY)
        self.process=None
        self.directory=None
        self.work=None
        self.sequence=0
        self.peak_rss=0
        self.storage_context=None

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()

    def _start(self):
        try:
            self.storage_context=hydrated_revision(self.root)
            self.storage_context.__enter__()
            env=child_environment(self.config,self.root)
            self.directory=tempfile.TemporaryDirectory(prefix='session-',dir=self.root)
            self.work=Path(self.directory.name)
            request=self.work/'session.json';response=self.work/'session-result.json'
            request.write_text(json.dumps({'operation':'serve','passage_ids':list(self.rows),
                                          'verified_model_manifest':verified_model_manifest()}))
            request.chmod(0o600)
        except BaseException as error:
            self.close()
            if isinstance(error,ArchiveStorageError):
                raise
            if isinstance(error,(OSError,ValueError)):
                raise CogneeRuntimeUnavailable('COGNEE_RUNTIME_UNAVAILABLE:LOCAL_PREFLIGHT_FAILED') from None
            raise
        try:
            self.process=subprocess.Popen([str(self.config.python),'-I',
                str(Path(__file__).with_name('cognee_runner.py')),str(request),str(response)],
                cwd=self.root,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,start_new_session=True)
        except OSError:
            self.close()
            raise CogneeRuntimeUnavailable('COGNEE_RUNTIME_UNAVAILABLE:CHILD_LAUNCH_FAILED') from None
        self.sequence=0
        self.peak_rss=0

    def close(self):
        process=self.process
        if process is not None:
            _quiesce_process_group(process,self.root)
        self.process=None
        try:
            if self.directory is not None:
                self.directory.cleanup()
        finally:
            self.directory=None
            self.work=None
            if self.storage_context is not None:
                self.storage_context.__exit__(None,None,None)
                self.storage_context=None

    def derive(self,row: dict) -> dict:
        if ((row['owner_id'],row['source_id'],row['revision_id'])!=self.scope
                or self.rows.get(row['passage_id'])!=(hashlib.sha256(row['text'].encode()).hexdigest(),row['role'])):
            raise ValueError('Cognee session source binding changed')
        if self.process is None or self.process.poll() is not None:
            self.close()
            with revision_lock(self.root):
                cached=_read_completed_receipt(self.config,row,self.root)
                if cached is not None:
                    return _persist_passage_result(self.config,row,cached,self.root)
            self._start()
        if self.sequence>=MAX_SESSION_PASSAGES:
            raise ValueError('Cognee session request budget exceeded')
        index=self.sequence;self.sequence+=1
        request=self.work/f'request-{index}.json';response=self.work/f'response-{index}.json'
        temporary=request.with_suffix('.tmp')
        temporary.write_text(json.dumps({'passage_id':row['passage_id'],'text':row['text'],'role':row['role']},ensure_ascii=False))
        temporary.chmod(0o600);os.replace(temporary,request)
        deadline=time.monotonic()+self.config.timeout_seconds
        try:
            while not response.is_file():
                if self.process.poll() is not None:
                    # The last response may be atomically published between
                    # the loop condition and observing the child's exit.
                    if response.is_file():
                        break
                    raise CogneeUnavailable('COGNEE_CHILD_FAILED')
                if time.monotonic()>=deadline:
                    raise CogneeUnavailable('COGNEE_TIMEOUT')
                status=Path('/proc')/str(self.process.pid)/'status'
                if status.is_file():
                    for line in status.read_text().splitlines():
                        if line.startswith('VmRSS:'):
                            self.peak_rss=max(self.peak_rss,int(line.split()[1])*1024)
                            if self.peak_rss>self.config.memory_bytes:
                                raise CogneeUnavailable('COGNEE_MEMORY_BUDGET')
                time.sleep(0.05)
            if response.stat().st_size>128_000:
                raise CogneeUnavailable('COGNEE_RESULT_INVALID')
            data=json.loads(response.read_text())
            if not isinstance(data,dict) or data.get('status')!='complete':
                error=data.get('error_code','unknown') if isinstance(data,dict) else 'unknown'
                raise _child_failure(error)
            previous=data.get('peak_rss_bytes',0)
            data['peak_rss_bytes']=max(self.peak_rss,previous if type(previous) is int else 0)
            return _persist_passage_result(self.config,row,data,self.root)
        except BaseException:
            self.close()
            raise


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
    order=('source_updated DESC NULLS LAST,revision_id,position,role,passage_id'
           if backlog else 'position,source_updated DESC NULLS LAST,role,passage_id')
    rows = conn.execute(f'''WITH candidates AS (
        SELECT p.passage_id,r.source_updated,r.revision_id,n.role,
          row_number() OVER (PARTITION BY r.revision_id,n.role ORDER BY p.passage_id) AS position
        FROM aster_archive.passages p JOIN aster_archive.nodes n USING(revision_id,node_id)
        JOIN aster_archive.conversation_revisions r USING(revision_id)
        JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
        WHERE c.owner_id=%s AND c.source_id=%s AND c.tombstoned_at IS NULL
          AND c.current_revision=r.revision_id AND n.role IN ('user','assistant')
          AND NOT EXISTS(SELECT 1 FROM aster_archive.cognee_jobs j
              WHERE j.passage_id=p.passage_id AND j.pipeline_key=%s))
        INSERT INTO aster_archive.cognee_jobs(passage_id,pipeline_key)
        SELECT passage_id,%s FROM candidates ORDER BY {order}
        LIMIT %s ON CONFLICT DO NOTHING RETURNING passage_id''',
        (owner_id,source_id,PIPELINE_KEY,PIPELINE_KEY,limit)).fetchall()
    return len(rows)


def _compact_completed_revisions(conn,owner_id,source_id,config,limit=4):
    """Bounded catch-up also runs on an idle worker after a compaction crash."""
    if not config.compact_completed:
        return 0
    # A LEFT JOIN includes unseeded passages. Never infer completion from only
    # the jobs already enqueued; a historical revision can be partly seeded.
    rows=conn.execute('''SELECT r.revision_id,count(*) AS eligible,
            count(*) FILTER(WHERE j.status='complete') AS completed
        FROM aster_archive.passages p
        JOIN aster_archive.nodes n USING(revision_id,node_id)
        JOIN aster_archive.conversation_revisions r USING(revision_id)
        LEFT JOIN aster_archive.cognee_jobs j ON j.passage_id=p.passage_id AND j.pipeline_key=%s
        WHERE r.owner_id=%s AND r.source_id=%s AND n.role IN ('user','assistant')
        GROUP BY r.revision_id
        HAVING count(*)=count(*) FILTER(WHERE j.status='complete')
        ORDER BY r.revision_id''',(PIPELINE_KEY,owner_id,source_id)).fetchall()
    packed=0
    for row in rows:
        root=config.root/'revisions'/stable_id(owner_id,source_id,row['revision_id'],PIPELINE_KEY)
        if not root.is_dir():
            continue
        # Completed archives are immutable and checked on hydration/backup
        # qualification. Do not rehash every previous graph on every batch.
        if ((root/MARKER_NAME).is_file() and (root/ARCHIVE_NAME).is_file()
                and not any((root/name).exists() or (root/name).is_symlink() for name in ROOTS)):
            continue
        compact_revision(root,eligible_passages=row['eligible'],completed_passages=row['completed'])
        packed+=1
        if packed>=limit:
            break
    return packed


def process_pending(store: PostgresArchive, owner_id: str, source_id: str,
                    archive_root: Path, config: CogneeConfig, limit=2, *, backlog=False, processor=None) -> dict:
    _identity(owner_id); _identity(source_id)
    if type(limit) is not int or not 1<=limit<=MAX_SESSION_PASSAGES:
        raise ValueError('invalid Cognee batch limit')
    minimum_free_bytes(); config.validate()
    knowledge = KnowledgeStore(store)
    with store.import_lock(owner_id,'cognee:' + stable_id(source_id,PIPELINE_KEY)) as conn:
        _compact_completed_revisions(conn,owner_id,source_id,config)
        if backlog:
            counts = conn.execute('''SELECT j.status,count(*) AS n FROM aster_archive.cognee_jobs j
                JOIN aster_archive.passages p USING(passage_id)
                JOIN aster_archive.conversation_revisions r USING(revision_id)
                WHERE r.owner_id=%s AND r.source_id=%s AND j.pipeline_key=%s GROUP BY j.status''',
                (owner_id,source_id,PIPELINE_KEY)).fetchall()
            values = {r['status']:r['n'] for r in counts}
            if values.get('complete',0)<5:
                raise CogneeUnavailable('COGNEE_SAMPLE_REQUIRED: complete at least five sample passages first')
            # The pilot gate is a prerequisite, not a global poison pill.
            # Exhausted items stay failed/visible while other passages progress.
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
            ORDER BY r.source_updated DESC NULLS LAST,r.revision_id,j.passage_id LIMIT %s''',(owner_id,source_id,PIPELINE_KEY,MAX_ATTEMPTS,limit)).fetchall()
        completed, failed = 0, 0
        grouped={}
        for row in jobs:
            grouped.setdefault((row['owner_id'],row['source_id'],row['revision_id']),[]).append(row)
        session=None;session_key=None
        with ExitStack() as stack:
            for row in jobs:
                require_storage_reserve(Path(archive_root))
                config.root.mkdir(parents=True,exist_ok=True,mode=0o700)
                require_storage_reserve(config.root,additional_bytes=64*1024**2)
                pid = row['passage_id']
                conn.execute('''UPDATE aster_archive.cognee_jobs SET status='running',attempts=attempts+1,started_at=now()
                    WHERE passage_id=%s AND pipeline_key=%s''',(pid,PIPELINE_KEY))
                try:
                    if processor is not None:
                        result=processor(config,row)
                    else:
                        scope_key=(row['owner_id'],row['source_id'],row['revision_id'])
                        if scope_key!=session_key:
                            if session is not None:
                                session.close()
                            session=stack.enter_context(ScopedCogneeSession(config,grouped[scope_key]))
                            session_key=scope_key
                        result=session.derive(row)
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
                    receipt = {k:result.get(k) for k in ('graph_nodes','graph_edges','chunks','candidate_edges','kept_edges','elapsed_seconds','peak_rss_bytes','manifest_sha256','runtime')}
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
                except (CogneeRuntimeUnavailable,ArchiveStorageError) as error:
                    # No passage inference started successfully. Keep earlier
                    # legitimate attempts, refund this shared-runtime attempt,
                    # and let the supervisor apply its bounded error backoff.
                    code=('local_cognee_storage_unavailable' if isinstance(error,ArchiveStorageError)
                          else 'local_cognee_runtime_unavailable')
                    conn.execute('''UPDATE aster_archive.cognee_jobs SET status='pending',
                        attempts=GREATEST(attempts-1,0),started_at=NULL,finished_at=now(),
                        error_code=%s
                        WHERE passage_id=%s AND pipeline_key=%s''',(code,pid,PIPELINE_KEY))
                    raise
                except (CogneeUnavailable,ValueError,KeyError):
                    conn.execute('''UPDATE aster_archive.cognee_jobs SET status=CASE WHEN attempts>=%s THEN 'failed' ELSE 'pending' END,
                        finished_at=now(),error_code='local_cognee_processing_failed' WHERE passage_id=%s AND pipeline_key=%s''',
                        (MAX_ATTEMPTS,pid,PIPELINE_KEY))
                    failed += 1
        # ExitStack has closed and reaped all children before any archive can
        # become authoritative. This stays inside the owner/source import lock.
        _compact_completed_revisions(conn,owner_id,source_id,config)
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
