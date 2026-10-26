"""Local-only configuration, bounded durable jobs, and an opt-in real CPU pilot."""
import json
import os
from pathlib import Path
import sys
import uuid

import pytest

from aster_archive.access import ArchiveScope
from aster_archive.cognee_bridge import (CogneeConfig,CogneeUnavailable,child_environment,
    process_pending,process_passage,coverage,PIPELINE_KEY,GLINER_REVISION,EMBEDDING_MODEL,
    verified_model_manifest,SAMPLE_LIMIT)
from aster_archive.knowledge import KnowledgeStore
from aster_archive.store import PostgresArchive,ArchiveNotFound


def config(tmp_path):
    models=tmp_path/'models';models.mkdir()
    (models/'manifest.json').write_text(json.dumps(verified_model_manifest()))
    return CogneeConfig(Path(sys.executable),tmp_path/'cognee',models)


def test_processing_environment_does_not_inherit_cloud_or_archive_credentials(tmp_path,monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','synthetic-secret')
    monkeypatch.setenv('COGNEE_API_KEY','synthetic-secret')
    monkeypatch.setenv('ASTER_ARCHIVE_DATABASE_URL','synthetic-db-secret')
    monkeypatch.setenv('COGNEE_ENV_FILE','/unrelated/settings.env')
    c=config(tmp_path);env=child_environment(c,c.root/'scope')
    assert env['OPENAI_API_KEY']==''
    assert 'COGNEE_API_KEY' not in env and 'ASTER_ARCHIVE_DATABASE_URL' not in env
    assert env['ASTER_COGNEE_OFFLINE']=='1' and env['HF_HUB_OFFLINE']=='1'
    assert env['ALLOW_HTTP_REQUESTS']=='false'
    assert env['GRAPH_EXTRACTOR']=='gliner_demo' and env['EMBEDDING_PROVIDER']=='fastembed'
    assert env['COGNEE_ENV_FILE'].startswith(str(c.root))
    assert env['GLINER_AUTO_INSTALL']=='false'


def test_changed_cognee_env_file_is_not_loaded(tmp_path):
    c=config(tmp_path);root=c.root/'scope';child_environment(c,root)
    (root/'local-only.env').write_text('LLM_API_KEY=synthetic-secret\n')
    with pytest.raises(ValueError):child_environment(c,root)


def test_missing_or_mismatched_models_fail_before_job_consumption(tmp_path):
    c=config(tmp_path)
    c.validate()
    (c.model_root/'manifest.json').write_text('{}')
    with pytest.raises(CogneeUnavailable):c.validate()
    (c.model_root/'manifest.json').unlink()
    with pytest.raises(CogneeUnavailable):c.validate()


def test_packaged_manifest_matches_verified_deploy_copy_from_any_working_directory(tmp_path,monkeypatch):
    from importlib import resources
    expected=(Path(__file__).resolve().parents[1]/'deploy/archive/cognee/model-manifest.json').read_bytes()
    monkeypatch.chdir(tmp_path)
    assert resources.files('aster_archive').joinpath('cognee-model-manifest.json').read_bytes()==expected
    assert verified_model_manifest()==json.loads(expected)


def test_cache_cannot_replace_its_own_trusted_model_hashes(tmp_path):
    c=config(tmp_path)
    data=verified_model_manifest()
    first=next(iter(data['files']))
    data['files'][first]='0'*64
    (c.model_root/'manifest.json').write_text(json.dumps(data))
    with pytest.raises(CogneeUnavailable):c.validate()
    from aster_archive.cognee_runner import verify_models
    with pytest.raises(ValueError,match='model_manifest_mismatch'):
        verify_models(c.model_root,verified_model_manifest())


def test_default_cognee_models_are_outside_mutable_archive(tmp_path,monkeypatch):
    monkeypatch.delenv('ASTER_ARCHIVE_COGNEE_ROOT',raising=False)
    monkeypatch.delenv('ASTER_ARCHIVE_COGNEE_MODEL_ROOT',raising=False)
    monkeypatch.setenv('ASTER_ARCHIVE_COGNEE_PYTHON',sys.executable)
    c=CogneeConfig.from_env()
    assert c.root==Path('/var/lib/aster-archive/cognee')
    assert c.model_root==Path('/var/lib/aster-archive-models/cognee')
    assert not c.model_root.is_relative_to(Path('/var/lib/aster-archive'))


def test_supervised_sigterm_terminates_active_cognee_process_group(tmp_path,monkeypatch):
    import signal
    from aster_archive import cognee_bridge
    from aster_archive.enrichment_worker import _ShutdownRequested
    killed=[]
    class Child:
        pid=123456
        calls=0
        def wait(self,timeout=None):
            self.calls+=1
            if self.calls==1:
                raise _ShutdownRequested()
            return -signal.SIGTERM
    child=Child()
    monkeypatch.setattr(cognee_bridge.subprocess,'Popen',lambda *a,**k:child)
    monkeypatch.setattr(cognee_bridge.os,'killpg',lambda pid,sig:killed.append((pid,sig)))
    c=config(tmp_path)
    with pytest.raises(_ShutdownRequested):
        cognee_bridge._run_child(c,{'operation':'derive'},c.root/'scope')
    assert killed==[(child.pid,signal.SIGTERM)] and child.calls==2


DSN=os.environ.get('ASTER_ARCHIVE_TEST_DSN')
requires_pg=pytest.mark.skipif(not DSN,reason='disposable PostgreSQL required')


def setup_archive(tmp_path, count=1):
    from aster_archive.ingest import import_path
    store=PostgresArchive(DSN);store.migrate();KnowledgeStore(store).migrate()
    owner='cognee-test-'+uuid.uuid4().hex;source='account';scope=ArchiveScope('test',owner,(source,),None)
    rows=[{'id':'conversation-'+str(i),'title':'Synthetic public-fact passage','update_time':10+i,'current_node':'u',
        'mapping':{'u':{'parent':None,'children':[],'message':{'id':'u','author':{'role':'user'},
            'content':{'content_type':'text','parts':['Marie Curie was born in Warsaw. She worked at the University of Paris with Pierre Curie.']}}}}}
        for i in range(count)]
    source_file=tmp_path/'export.json';source_file.write_text(json.dumps(rows))
    root=tmp_path/'archive';import_path(store,source_file,root,owner,source)
    return store,scope,root


def synthetic_processor(c,row):
    return {'status':'complete','summaries':['person: Marie Curie; place: Warsaw'],
            'graph_nodes':2,'graph_edges':1,'chunks':1,'candidate_edges':1,'kept_edges':1,'elapsed_seconds':0.1}


@requires_pg
def test_sample_cap_replay_and_scope(tmp_path):
    store,scope,root=setup_archive(tmp_path,count=25);c=config(tmp_path)
    assert process_pending(store,scope.owner_id,'account',root,c,limit=20,processor=synthetic_processor)['completed']==SAMPLE_LIMIT
    assert process_pending(store,scope.owner_id,'account',root,c,limit=20,processor=synthetic_processor)['completed']==0
    state=coverage(store,scope)
    assert state['jobs'][0]['passages']==SAMPLE_LIMIT
    knowledge=KnowledgeStore(store);hits=knowledge.search(scope,'Curie',kind='cognee_summary')['results']
    assert len(hits)==SAMPLE_LIMIT and all(r['metadata']['model_output_is_unverified'] for r in hits)
    denied=ArchiveScope('other','other-owner',None,None)
    assert knowledge.search(denied,'Curie')['results']==[]
    assert coverage(store,denied)['jobs']==[]
    with pytest.raises(ArchiveNotFound):knowledge.fetch(denied,hits[0]['id'])
    assert process_pending(store,scope.owner_id,'account',root,c,limit=20,backlog=True,processor=synthetic_processor)['completed']==25-SAMPLE_LIMIT


@requires_pg
def test_bounded_failure_receipt_and_no_silent_derived_success(tmp_path):
    store,scope,root=setup_archive(tmp_path);c=config(tmp_path)
    def fail(*args):raise CogneeUnavailable('synthetic local failure')
    for _ in range(3):
        result=process_pending(store,scope.owner_id,'account',root,c,processor=fail)
        assert result['failed']==1
    assert process_pending(store,scope.owner_id,'account',root,c,processor=fail)['failed']==0
    assert coverage(store,scope)['jobs']==[{'status':'failed','error_code':'local_cognee_processing_failed','passages':1,'summaries':0}]
    assert KnowledgeStore(store).search(scope,'Curie')['results']==[]
    with pytest.raises(CogneeUnavailable):process_pending(store,scope.owner_id,'account',root,c,backlog=True,processor=synthetic_processor)


@requires_pg
def test_missing_models_leave_cognee_outbox_empty(tmp_path):
    store,scope,root=setup_archive(tmp_path);c=config(tmp_path)
    (c.model_root/'manifest.json').unlink()
    with pytest.raises(CogneeUnavailable):process_pending(store,scope.owner_id,'account',root,c,processor=synthetic_processor)
    assert coverage(store,scope)['jobs']==[]


@requires_pg
@pytest.mark.skipif(not os.environ.get('ASTER_COGNEE_TEST_PYTHON') or not os.environ.get('ASTER_COGNEE_TEST_MODELS'),
    reason='explicit isolated CPU runtime and prepared local models required')
def test_real_cognee_graph_is_checkpointed_after_database_ack_crash(tmp_path):
    store,scope,root=setup_archive(tmp_path)
    c=CogneeConfig(Path(os.environ['ASTER_COGNEE_TEST_PYTHON']),tmp_path/'real-cognee',Path(os.environ['ASTER_COGNEE_TEST_MODELS']))
    def after_child_crash(config,row):
        result=process_passage(config,row)
        assert result['chunks']>=1 and result['summaries']
        raise RuntimeError('synthetic DB-ack interruption after actual child completion')
    with pytest.raises(RuntimeError):
        process_pending(store,scope.owner_id,'account',root,c,limit=1,processor=after_child_crash)
    assert KnowledgeStore(store).search(scope,'Curie')['results']==[]
    assert process_pending(store,scope.owner_id,'account',root,c,limit=1)['completed']==1
    hits=KnowledgeStore(store).search(scope,'Curie',kind='cognee_summary')['results']
    assert hits and 'Curie' in hits[0]['text']
    assert hits[0]['metadata']['verified_decision'] is False
    assert hits[0]['citations'][0]['exact_quote'].startswith('Marie Curie')
    assert process_pending(store,scope.owner_id,'account',root,c,limit=1)['completed']==0
    with store.connection(readonly=True) as conn:
        row=conn.execute('''SELECT j.* FROM aster_archive.cognee_jobs j
            JOIN aster_archive.passages p USING(passage_id)
            JOIN aster_archive.conversation_revisions r USING(revision_id)
            WHERE r.owner_id=%s AND j.pipeline_key=%s''',(scope.owner_id,PIPELINE_KEY)).fetchone()
    assert row['status']=='complete' and row['attempts']==2
    assert row['receipt']['chunks']>=1 and row['receipt']['manifest_sha256']
