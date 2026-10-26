"""Scoped child reuse, source binding and durable per-passage recovery."""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from aster_archive import cognee_bridge as bridge, cognee_runner as runner


@pytest.fixture
def config(tmp_path):
    models=tmp_path/'models';models.mkdir()
    (models/'manifest.json').write_text(json.dumps(bridge.verified_model_manifest()))
    return bridge.CogneeConfig(Path(sys.executable),tmp_path/'cognee',models)


def row(number,text=None,revision='b'*64):
    return {'owner_id':'synthetic-owner','source_id':'synthetic-source','revision_id':revision,
            'passage_id':f'{number:064x}','role':'user','text':text or f'Passage {number}'}


def test_session_rejects_mixed_revision_owner_source_and_changed_text(config):
    first=row(1)
    for field,value in [('revision_id','c'*64),('owner_id','other'),('source_id','other')]:
        with pytest.raises(ValueError,match='one immutable revision'):
            bridge.ScopedCogneeSession(config,[first,row(2)|{field:value}])
    with bridge.ScopedCogneeSession(config,[first]) as session:
        with pytest.raises(ValueError,match='source binding changed'):
            session.derive(first|{'text':'Different evidence'})
        assert session.process is None


def test_explicit_runtime_budgets_and_environment(config,monkeypatch):
    values={'PYTHON':sys.executable,'ROOT':str(config.root),'MODEL_ROOT':str(config.model_root),
            'CPU_THREADS':'12','MEMORY_BYTES':str(8*1024**3),'DEVICE':'cuda',
            'GPU_MEMORY_FRACTION':'0.85','TIMEOUT_SECONDS':'1200','INFERENCE_BATCH_SIZE':'8'}
    for key,value in values.items():
        monkeypatch.setenv('ASTER_ARCHIVE_COGNEE_'+key,value)
    actual=bridge.CogneeConfig.from_env();actual.validate()
    assert actual.cpu_threads==12 and actual.device=='cuda' and actual.inference_batch_size==8
    env=bridge.child_environment(actual,actual.root/'scope')
    assert env['ASTER_COGNEE_GPU_MEMORY_FRACTION']=='0.85'
    assert env['ASTER_COGNEE_CPU_THREADS']=='12'
    for field,value in [('device','auto'),('gpu_memory_fraction',1.0),('inference_batch_size',17),('cpu_threads',65)]:
        with pytest.raises(ValueError):replace(actual,**{field:value}).validate()


def test_unexecutable_interpreter_is_rejected_before_jobs(config,tmp_path):
    interpreter=tmp_path/'interpreter';interpreter.write_text('synthetic')
    interpreter.chmod(0o600)
    with pytest.raises(ValueError,match='executable'):
        replace(config,python=interpreter).validate()


def test_launch_oserror_is_global_redacted_and_cleans_session(config,monkeypatch):
    def unavailable(*args,**kwargs):
        raise PermissionError('PRIVATE interpreter /secret/path')
    monkeypatch.setattr(bridge.subprocess,'Popen',unavailable)
    with bridge.ScopedCogneeSession(config,[row(1)]) as session:
        with pytest.raises(bridge.CogneeRuntimeUnavailable) as failure:
            session.derive(row(1))
        assert str(failure.value)=='COGNEE_RUNTIME_UNAVAILABLE:CHILD_LAUNCH_FAILED'
        assert session.process is None and session.directory is None
        assert not list(session.root.glob('session-*'))


def test_requested_cuda_never_falls_back_to_cpu(config,monkeypatch):
    monkeypatch.setenv('ASTER_COGNEE_DEVICE','cuda')
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(cuda=SimpleNamespace(is_available=lambda:False)))
    with pytest.raises(RuntimeError,match='requested_cuda_unavailable'):
        asyncio.run(runner._initialize_runtime({'manifest':bridge.verified_model_manifest()},config.model_root))


def test_runtime_failure_codes_are_shared_bounded_and_do_not_expose_messages():
    assert bridge.RUNTIME_FAILURE_CODES==runner._RUNTIME_CONFIGURATION_CODES
    for code in bridge.RUNTIME_FAILURE_CODES:
        assert isinstance(bridge._child_failure(code),bridge.CogneeRuntimeUnavailable)
    ordinary=bridge._child_failure('RuntimeError')
    assert isinstance(ordinary,bridge.CogneeUnavailable)
    assert not isinstance(ordinary,bridge.CogneeRuntimeUnavailable)
    private=bridge._child_failure('PRIVATE source text /secret/path')
    assert str(private)=='COGNEE_CHILD_FAILED:unknown'


def test_initialization_failure_is_global_and_redacted(config,tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner,'verify_models',lambda *args:bridge.verified_model_manifest())
    async def unavailable(*args):
        raise RuntimeError('PRIVATE driver exception /secret/path')
    monkeypatch.setattr(runner,'_initialize_runtime',unavailable)
    with pytest.raises(runner.RuntimeInitializationFailure) as failure:
        asyncio.run(runner.derive(row(1)|{'verified_model_manifest':bridge.verified_model_manifest()},config.model_root))
    assert runner._safe_error_code(failure.value)=='RUNTIME_INITIALIZATION_FAILED'
    assert 'PRIVATE' not in str(failure.value)
    def damaged(*args):raise ValueError('PRIVATE model /secret/path')
    monkeypatch.setattr(runner,'verify_models',damaged)
    with pytest.raises(runner.RuntimeInitializationFailure) as failure:
        asyncio.run(runner.derive(row(1)|{'verified_model_manifest':bridge.verified_model_manifest()},config.model_root))
    assert runner._safe_error_code(failure.value)=='MODEL_VERIFICATION_FAILED'


def test_global_failure_reaches_supervisor_backoff_without_passage_failures(config,tmp_path,monkeypatch):
    from aster_archive import enrichment_worker as worker
    events=[];calls=[]
    def unavailable(*args,**kwargs):
        calls.append(1)
        raise bridge.CogneeRuntimeUnavailable('COGNEE_RUNTIME_UNAVAILABLE:CUDA_UNAVAILABLE')
    class Stop:
        def __init__(self):self.polls=[]
        def is_set(self):return len(self.polls)>=2
        def wait(self,seconds):self.polls.append(seconds)
        def set(self):pass
    stop=Stop()
    monkeypatch.setattr(worker,'process_pending',unavailable)
    result=worker.run_worker('cognee',object(),'owner','source',tmp_path,
        config=config,stop=stop,poll_seconds=7,emit=events.append)
    assert len(calls)==2 and stop.polls==[7,7]
    assert result['batch_errors']==2 and result['failed']==0 and result['completed']==0
    assert [event['error_code'] for event in events if event['event']=='batch_error']==['LOCAL_COGNEE_UNAVAILABLE']*2


@pytest.fixture
def synthetic_child(tmp_path,monkeypatch):
    script=tmp_path/'synthetic_child.py'
    script.write_text('''import hashlib,json,os,pathlib,sys,time
work=pathlib.Path(sys.argv[1]).parent
root=pathlib.Path.cwd();receipts=root/'receipts';receipts.mkdir(exist_ok=True)
manifest=hashlib.sha256((pathlib.Path(os.environ['ASTER_COGNEE_MODEL_ROOT'])/'manifest.json').read_bytes()).hexdigest()
for index in range(20):
    request=work/f'request-{index}.json'
    while not request.exists():time.sleep(.01)
    item=json.loads(request.read_text())
    if item['text']=='CRASH':sys.exit(21)
    if item['text']=='SLOW':time.sleep(30)
    target=receipts/(item['passage_id']+'.json')
    if target.exists():
        result=json.loads(target.read_text());result['reused_completed_receipt']=True
    else:
        result={'status':'complete','summaries':[item['text']],
                'source_sha256':hashlib.sha256(item['text'].encode()).hexdigest(),
                'manifest_sha256':manifest,'runtime':{'runner_pid':os.getpid()},'elapsed_seconds':.1}
        target.write_text(json.dumps(result))
    response=work/f'response-{index}.json';temporary=response.with_suffix('.tmp')
    temporary.write_text(json.dumps(result));os.replace(temporary,response)
''')
    original=subprocess.Popen
    children=[]
    def launch(command,**kwargs):
        command=list(command);command[2]=str(script)
        process=original(command,**kwargs);children.append(process)
        return process
    monkeypatch.setattr(bridge.subprocess,'Popen',launch)
    return children


def test_session_reuses_one_child_and_replays_after_child_death(config,synthetic_child):
    rows=[row(1),row(2),row(3)]
    with bridge.ScopedCogneeSession(config,rows) as session:
        first=session.derive(rows[0]);second=session.derive(rows[1])
        assert first['runtime']['runner_pid']==second['runtime']['runner_pid']
        assert len(synthetic_child)==1
        session.process.kill();session.process.wait()
        replay=session.derive(rows[0]);third=session.derive(rows[2])
        assert replay['reused_completed_receipt'] is True
        assert replay['summaries']==[rows[0]['text']]
        assert third['runtime']['runner_pid']!=first['runtime']['runner_pid']
        assert len(synthetic_child)==2
    assert all(child.poll() is not None for child in synthetic_child)
    assert len(list(session.root.glob('receipts/*.json')))==3
    assert not list(session.root.glob('session-*'))


def test_failed_child_does_not_destroy_prior_receipt_or_next_passage(config,synthetic_child):
    rows=[row(1),row(2,'CRASH'),row(3)]
    with bridge.ScopedCogneeSession(config,rows) as session:
        session.derive(rows[0])
        with pytest.raises(bridge.CogneeUnavailable,match='CHILD_FAILED'):
            session.derive(rows[1])
        assert (session.root/'receipts'/(rows[0]['passage_id']+'.json')).is_file()
        result=session.derive(rows[2])
        assert result['summaries']==[rows[2]['text']]
    assert len(synthetic_child)==2


def test_child_exit_cannot_hide_atomically_published_response(config,tmp_path,monkeypatch):
    item=row(1)
    with bridge.ScopedCogneeSession(config,[item]) as session:
        def start():
            session.root.mkdir(parents=True)
            (session.root/'receipts').mkdir()
            session.work=tmp_path/'channel';session.work.mkdir()
            class Child:
                pid=123456
                def poll(self):
                    response=session.work/'response-0.json'
                    if not response.exists():
                        value={'status':'complete','summaries':[item['text']],
                            'source_sha256':hashlib.sha256(item['text'].encode()).hexdigest(),
                            'manifest_sha256':hashlib.sha256((config.model_root/'manifest.json').read_bytes()).hexdigest()}
                        temporary=response.with_suffix('.tmp')
                        temporary.write_text(json.dumps(value));os.replace(temporary,response)
                    return 0
                def wait(self,timeout=None):return 0
            session.process=Child()
        monkeypatch.setattr(session,'_start',start)
        result=session.derive(item)
        assert result['summaries']==[item['text']]


def test_sigterm_unwinds_reused_child_without_losing_completed_receipt(config,synthetic_child,monkeypatch):
    from aster_archive.enrichment_worker import _ShutdownRequested
    rows=[row(1),row(2,'SLOW')]
    with bridge.ScopedCogneeSession(config,rows) as session:
        session.derive(rows[0])
        original_sleep=bridge.time.sleep
        interrupted=False
        def interrupt_once(seconds):
            nonlocal interrupted
            if not interrupted:
                interrupted=True
                raise _ShutdownRequested()
            original_sleep(seconds)
        monkeypatch.setattr(bridge.time,'sleep',interrupt_once)
        with pytest.raises(_ShutdownRequested):session.derive(rows[1])
        assert session.process is None
        assert (session.root/'receipts'/(rows[0]['passage_id']+'.json')).is_file()
    assert all(child.poll() is not None for child in synthetic_child)


def test_runner_verifies_models_once_per_session_and_keeps_passage_receipts(config,tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    counts={'verified':0,'initialized':0};recalls=[]
    manifest=bridge.verified_model_manifest()
    def verify(*args):counts['verified']+=1;return manifest
    async def add(*args,**kwargs):return SimpleNamespace(status='PipelineRunCompleted')
    async def pipeline(*args,**kwargs):return SimpleNamespace(status='PipelineRunCompleted')
    async def recall(text,**kwargs):
        recalls.append((text,kwargs['datasets']))
        return [SimpleNamespace(source='graph',text='entity: '+text)]
    async def wait():pass
    async def initialize(runtime,model_root):
        counts['initialized']+=1
        runtime.update(initialized=True,provenance={'execution_device':'cpu'},cognee=SimpleNamespace(
            add=add,run_custom_pipeline=pipeline,recall=recall,wait_for_background_tasks=wait,
            SearchType=SimpleNamespace(SUMMARIES='summaries')))
    async def tasks(**kwargs):return []
    class Stats:
        nodes=1;kept_edges=0;chunks=1;candidate_edges=0
    monkeypatch.setattr(runner,'verify_models',verify)
    monkeypatch.setattr(runner,'_initialize_runtime',initialize)
    monkeypatch.setitem(sys.modules,'cognee.tasks.graph.gliner_demo.tasks',SimpleNamespace(
        get_gliner_demo_tasks=tasks,GlinerRunStats=Stats))
    runtime={}
    for item in [row(1),row(2)]:
        payload=item|{'verified_model_manifest':manifest}
        result=asyncio.run(runner.derive(payload,config.model_root,runtime=runtime))
        assert result['summaries']==['entity: '+item['text']]
        assert result['source_sha256']==hashlib.sha256(item['text'].encode()).hexdigest()
    assert counts=={'verified':1,'initialized':1}
    assert [datasets for _text,datasets in recalls]==[['archive_passage_'+row(1)['passage_id']],['archive_passage_'+row(2)['passage_id']]]
    replay=asyncio.run(runner.derive(row(1)|{'verified_model_manifest':manifest},config.model_root,runtime={}))
    assert replay['reused_completed_receipt'] is True
    assert counts=={'verified':2,'initialized':1}
