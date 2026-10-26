#!/usr/bin/env python3
"""Read-only enrichment completion proof; optional private progress checkpoints.

Execute inside the existing MCP reader container, without loading models or
altering workers. Every database read shares one repeatable-read snapshot.
Only counts, public pipeline identities and a corpus fingerprint leave it.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

SCHEMA_VERSION = 1
STALL_SECONDS = 2 * 60 * 60
MAX_RESULT_BYTES = 65536
IMPORT_STATES = ('pending', 'running', 'complete', 'complete_with_errors', 'failed')
EMBEDDING_COUNTS = ('current_passages', 'embedded_passages', 'unique_texts',
    'embedded_unique_texts', 'pending_unique_texts', 'running_unique_texts',
    'failed_unique_texts', 'unseeded_unique_texts')
COGNEE_COUNTS = ('eligible_passages', 'completed_passages', 'pending_passages',
    'running_passages', 'failed_passages', 'unseeded_passages')

# Kept independent of host Python packages. The reader image supplies psycopg
# and archive modules; neither of the imported identity functions loads models.
INNER = r"""
import hashlib,json,os,sys
from aster_archive.access import ArchiveScope
from aster_archive.store import PostgresArchive
from aster_archive.embedding_store import EmbeddingStore
from aster_archive.embeddings import generation_descriptor,generation_id
from aster_archive.cognee_bridge import PIPELINE_KEY

def snapshot(owner,source):
    scope=ArchiveScope('local-completion-probe',owner,(source,),None)
    store=PostgresArchive(os.environ['ASTER_ARCHIVE_DATABASE_URL'])
    identity=generation_id(generation_descriptor())
    with store.connection(readonly=True) as conn:
        observed=conn.execute('SELECT now() AS observed').fetchone()['observed'].isoformat()
        versions=conn.execute('SELECT version FROM aster_archive.schema_version').fetchall()
        if versions != [{'version':1}]:
            raise ValueError('schema')
        corpus=conn.execute('''SELECT count(*) AS conversations,
            count(*) FILTER(WHERE current_ambiguous) AS ambiguous_current_revisions,
            count(*) FILTER(WHERE current_revision IS NULL) AS missing_current_revisions
            FROM aster_archive.conversations WHERE owner_id=%s AND source_id=%s
              AND tombstoned_at IS NULL''',(owner,source)).fetchone()
        digest=hashlib.sha256()
        digest.update(json.dumps([owner,source,identity,PIPELINE_KEY],
                                 separators=(',',':')).encode())
        with conn.cursor(name='completion_fingerprint') as cursor:
            cursor.execute('''SELECT conversation_id,current_revision,current_ambiguous
                FROM aster_archive.conversations WHERE owner_id=%s AND source_id=%s
                AND tombstoned_at IS NULL ORDER BY conversation_id''',(owner,source))
            while True:
                rows=cursor.fetchmany(1000)
                if not rows:break
                for row in rows:
                    digest.update(b'\n')
                    digest.update(json.dumps([row['conversation_id'],row['current_revision'],
                        row['current_ambiguous']],separators=(',',':')).encode())
        imports={state:0 for state in ('pending','running','complete','complete_with_errors','failed')}
        with conn.cursor(name='completion_import_fingerprint') as cursor:
            cursor.execute('''SELECT import_id,status FROM aster_archive.imports
                WHERE owner_id=%s AND source_id=%s ORDER BY import_id''',(owner,source))
            while True:
                rows=cursor.fetchmany(1000)
                if not rows:break
                for row in rows:
                    if row['status'] not in imports:raise ValueError('import state')
                    imports[row['status']]+=1
                    digest.update(b'\nimport:')
                    digest.update(json.dumps([row['import_id'],row['status']],
                                             separators=(',',':')).encode())
        embedding=EmbeddingStore(store)._coverage(conn,scope,identity)
        embedding.pop('basis',None)
        embedding.pop('complete',None)
        cognee=conn.execute('''SELECT count(*) AS eligible_passages,
            count(*) FILTER(WHERE j.status='complete') AS completed_passages,
            count(*) FILTER(WHERE j.status='pending') AS pending_passages,
            count(*) FILTER(WHERE j.status='running') AS running_passages,
            count(*) FILTER(WHERE j.status='failed') AS failed_passages,
            count(*) FILTER(WHERE j.passage_id IS NULL) AS unseeded_passages
            FROM aster_archive.passages p
            JOIN aster_archive.nodes n USING(revision_id,node_id)
            JOIN aster_archive.conversation_revisions r USING(revision_id)
            JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
            LEFT JOIN aster_archive.cognee_jobs j ON j.passage_id=p.passage_id AND j.pipeline_key=%s
            WHERE c.owner_id=%s AND c.source_id=%s AND c.tombstoned_at IS NULL
              AND c.current_revision=r.revision_id AND n.role IN ('user','assistant')''',
            (PIPELINE_KEY,owner,source)).fetchone()
        cognee['pipeline_key']=PIPELINE_KEY
    return {'schema_version':1,'database_schema_version':1,'observed_at':observed,
        'corpus_fingerprint':digest.hexdigest(),'corpus':corpus,'imports':imports,
        'embeddings':embedding,'cognee':cognee}

try:
    print(json.dumps(snapshot(sys.argv[1],sys.argv[2]),allow_nan=False))
except Exception:
    print(json.dumps({'error_code':'READ_ONLY_SNAPSHOT_FAILED'}))
    raise SystemExit(1)
"""


class ProbeError(Exception):
    """Only fixed public error codes may be reported."""


def fail(code):
    raise ProbeError(code)


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        fail('INVALID_STATUS')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        fail('INVALID_STATUS')
    return result.astimezone(timezone.utc)


def counts(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        fail('INVALID_STATUS')
    if any(type(v) is not int or not 0 <= v <= 2**63-1 for v in value.values()):
        fail('INVALID_STATUS')


def classify(value):
    """Validate the bounded wire schema and derive strict completion flags."""
    expected = {'schema_version','database_schema_version','observed_at',
                'corpus_fingerprint','corpus','imports','embeddings','cognee'}
    if not isinstance(value, dict) or set(value) != expected:
        fail('INVALID_STATUS')
    if type(value['schema_version']) is not int or value['schema_version'] != SCHEMA_VERSION:
        fail('INVALID_STATUS')
    if type(value['database_schema_version']) is not int or value['database_schema_version'] != 1:
        fail('INVALID_STATUS')
    timestamp(value['observed_at'])
    if not re.fullmatch(r'[a-f0-9]{64}', value['corpus_fingerprint']):
        fail('INVALID_STATUS')
    corpus, imports = value['corpus'], value['imports']
    counts(corpus, ('conversations','ambiguous_current_revisions','missing_current_revisions'))
    if any(corpus[k] > corpus['conversations'] for k in ('ambiguous_current_revisions','missing_current_revisions')):
        fail('INVALID_STATUS')
    counts(imports, IMPORT_STATES)
    embedding, cognee = value['embeddings'], value['cognee']
    if not isinstance(embedding, dict) or not re.fullmatch(r'[a-f0-9]{64}', embedding.get('generation_id', '')):
        fail('INVALID_STATUS')
    counts({k:v for k,v in embedding.items() if k != 'generation_id'}, EMBEDDING_COUNTS)
    if not isinstance(cognee, dict) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', cognee.get('pipeline_key', '')):
        fail('INVALID_STATUS')
    counts({k:v for k,v in cognee.items() if k != 'pipeline_key'}, COGNEE_COUNTS)
    if (embedding['embedded_passages'] > embedding['current_passages']
            or embedding['unique_texts'] > embedding['current_passages']
            or embedding['embedded_unique_texts'] > embedding['unique_texts']
            or embedding['embedded_unique_texts'] > embedding['embedded_passages']
            or sum(embedding[k] for k in EMBEDDING_COUNTS[4:]) > embedding['unique_texts']
            or sum(cognee[k] for k in COGNEE_COUNTS[1:]) != cognee['eligible_passages']
            or cognee['eligible_passages'] > embedding['current_passages']):
        fail('INVALID_STATUS')
    embedding['remaining_passages'] = embedding['current_passages'] - embedding['embedded_passages']
    embedding['complete'] = (embedding['current_passages'] > 0
        and embedding['remaining_passages'] == 0
        and embedding['unique_texts'] == embedding['embedded_unique_texts']
        and all(embedding[k] == 0 for k in EMBEDDING_COUNTS[4:]))
    cognee['remaining_passages'] = cognee['eligible_passages'] - cognee['completed_passages']
    cognee['complete'] = cognee['eligible_passages'] > 0 and cognee['remaining_passages'] == 0
    value['imports_complete'] = imports['complete'] > 0 and all(imports[k] == 0 for k in IMPORT_STATES if k != 'complete')
    value['complete'] = (corpus['conversations'] > 0 and corpus['ambiguous_current_revisions'] == 0
        and corpus['missing_current_revisions'] == 0 and value['imports_complete']
        and embedding['complete'] and cognee['complete'])
    return value


def probe(owner, source, container):
    if (any(not isinstance(v,str) or not v or len(v)>512 or '\x00' in v for v in (owner,source))
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',container)):
        fail('INVALID_ARGUMENTS')
    result = subprocess.run(['docker','exec','-i',container,'python','-',owner,source],
        input=INNER.encode(),stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=90,check=False)
    if result.returncode or not 1 <= len(result.stdout) <= MAX_RESULT_BYTES:
        fail('READ_ONLY_SNAPSHOT_FAILED')
    return classify(json.loads(result.stdout))


def progress(current, previous=None):
    """Reset baselines for a new corpus; never infer a stall on first sight."""
    observed = timestamp(current['observed_at'])
    if previous is not None and timestamp(previous['observed_at']) > observed:
        fail('OBSERVATION_TIME_REGRESSED')
    same = (isinstance(previous,dict) and previous.get('schema_version') == SCHEMA_VERSION
            and previous.get('corpus_fingerprint') == current['corpus_fingerprint'])
    result = {}
    for component, completed_key in (('embeddings','embedded_passages'),('cognee','completed_passages')):
        completed = current[component][completed_key]
        last = current['observed_at']
        if same:
            old = previous[component][completed_key]
            if completed < old:
                fail('PROGRESS_REGRESSED')
            if completed == old:
                last = previous['progress'][component]['last_progress_at']
                if timestamp(last) > observed:
                    fail('OBSERVATION_TIME_REGRESSED')
        stalled = (current[component]['remaining_passages'] > 0
                   and (observed-timestamp(last)).total_seconds() >= STALL_SECONDS)
        result[component] = {'last_progress_at':last,'stalled':stalled}
    current['progress'] = result
    current['stalled'] = any(row['stalled'] for row in result.values())
    return current


def private_directory(path):
    path=Path(path)
    if os.geteuid() != 0:
        fail('ROOT_REQUIRED_FOR_STATE')
    if not path.is_absolute() or path.resolve() != path:
        fail('UNSAFE_STATE_DIRECTORY')
    path.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        fail('UNSAFE_STATE_DIRECTORY')
    return path


def private_open(path, flags):
    descriptor=os.open(path,flags | os.O_NOFOLLOW,0o600)
    info=os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o077 or info.st_nlink != 1):
        os.close(descriptor)
        fail('UNSAFE_STATE_FILE')
    return descriptor


def save_progress(current, state_dir):
    directory=private_directory(state_dir)
    with os.fdopen(private_open(directory/'status.lock',os.O_RDWR|os.O_CREAT),'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        previous=None
        try:
            descriptor=private_open(directory/'latest.json',os.O_RDONLY)
        except FileNotFoundError:
            pass
        else:
            with os.fdopen(descriptor,'rb') as stream:
                raw=stream.read(MAX_RESULT_BYTES+1)
            if len(raw)>MAX_RESULT_BYTES:fail('INVALID_PREVIOUS_STATUS')
            previous=json.loads(raw)
        current=progress(current,previous)
        raw=(json.dumps(current,sort_keys=True,separators=(',',':'),allow_nan=False)+'\n').encode()
        with os.fdopen(private_open(directory/'history.ndjson',os.O_WRONLY|os.O_APPEND|os.O_CREAT),'ab') as history:
            history.write(raw);history.flush();os.fsync(history.fileno())
        descriptor,temporary=tempfile.mkstemp(prefix='.latest-',dir=directory)
        try:
            with os.fdopen(descriptor,'wb') as stream:
                stream.write(raw);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,directory/'latest.json')
            parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(parent)
            finally:os.close(parent)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)
    return current


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner',required=True)
    parser.add_argument('--source',required=True)
    parser.add_argument('--container',default='aster-archive-mcp-1')
    parser.add_argument('--state-dir',type=Path)
    args=parser.parse_args(argv)
    try:
        current=probe(args.owner,args.source,args.container)
        if args.state_dir is not None:
            current=save_progress(current,args.state_dir)
        print(json.dumps(current,sort_keys=True,allow_nan=False))
        return 0
    except ProbeError as error:
        code=str(error)
    except subprocess.TimeoutExpired:
        code='READ_ONLY_SNAPSHOT_TIMEOUT'
    except Exception:
        code='STATUS_PROBE_FAILED'
    print(json.dumps({'schema_version':SCHEMA_VERSION,'complete':False,'error_code':code}))
    return 1


if __name__=='__main__':
    raise SystemExit(main())
