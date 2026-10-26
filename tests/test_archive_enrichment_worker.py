"""The supervisor preserves component job bounds, privacy and interruption."""
import json
import os
from pathlib import Path
import signal
import threading
import uuid

import pytest

from aster_archive import enrichment_worker as worker
from aster_archive.cognee_bridge import CogneeUnavailable


class StopAfterPolls:
    def __init__(self, count):
        self.count = count
        self.polls = []
        self.stopped = False

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.polls.append(seconds)
        if len(self.polls) >= self.count:
            self.set()


class PreparedConfig:
    def validate(self):
        pass


def test_continuous_worker_bounds_batches_and_does_not_log_component_text(tmp_path,monkeypatch):
    calls, events = [], []
    def derive(store, owner, source, root, *, limit):
        calls.append((owner,source,root,limit))
        return {'completed':2,'failed':0,'text':'PRIVATE HISTORICAL CONTENT','error':'SECRET'}
    monkeypatch.setattr(worker,'derive_pending',derive)
    stop=StopAfterPolls(2)
    result=worker.run_worker('knowledge',object(),'owner','source',tmp_path,
                             batch_size=2,poll_seconds=7,stop=stop,emit=events.append)
    assert len(calls)==2 and all(row[-1]==2 for row in calls)
    assert stop.polls==[0,0] and result['completed']==4
    assert result['status']=='stopped'
    assert 'PRIVATE' not in json.dumps(events) and 'SECRET' not in json.dumps(events)


@pytest.mark.parametrize('options',[{'batch_size':0},{'batch_size':True},{'poll_seconds':0},
    {'poll_seconds':True},{'work_pause_seconds':-1},{'work_pause_seconds':True},
    {'work_pause_seconds':3601},{'backlog':True}])
def test_invalid_worker_settings_cannot_consume_pending_job(tmp_path,monkeypatch,options):
    calls=[]
    monkeypatch.setattr(worker,'derive_pending',lambda *a,**k:calls.append(1))
    with pytest.raises(ValueError):
        worker.run_worker('knowledge',object(),'owner','source',tmp_path,once=True,**options)
    assert calls==[]


def test_cognee_accepts_hundred_passage_batch_but_rejects_over_budget(tmp_path,monkeypatch):
    calls=[]
    def process(*args,limit,backlog):
        calls.append(limit)
        return {'completed':limit,'failed':0}
    monkeypatch.setattr(worker,'process_pending',process)
    result=worker.run_worker('cognee',object(),'owner','source',tmp_path,
        config=PreparedConfig(),batch_size=100,once=True,emit=lambda event:None)
    assert calls==[100] and result['completed']==100
    with pytest.raises(ValueError,match='batch size'):
        worker.run_worker('cognee',object(),'owner','source',tmp_path,
            config=PreparedConfig(),batch_size=101,once=True,emit=lambda event:None)
    assert calls==[100]


def test_invalid_storage_reserve_cannot_consume_pending_job(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(worker,'derive_pending',lambda *a,**k:calls.append(1))
    monkeypatch.setenv('ASTER_ARCHIVE_MIN_FREE_BYTES','malformed')
    with pytest.raises(ValueError):
        worker.run_worker('knowledge',object(),'owner','source',tmp_path,once=True)
    assert calls==[]


def test_unprepared_models_fail_before_polling_or_consuming_jobs(tmp_path,monkeypatch):
    calls=[]
    class MissingModels:
        def validate(self):
            raise CogneeUnavailable('synthetic secret path')
    monkeypatch.setattr(worker,'process_pending',lambda *a,**k:calls.append(1))
    with pytest.raises(CogneeUnavailable):
        worker.run_worker('cognee',object(),'owner','source',tmp_path,once=True,config=MissingModels())
    assert calls==[]


def test_idle_sample_waits_without_enabling_backlog(tmp_path,monkeypatch):
    calls,events=[],[]
    def process(*args,limit,backlog):
        calls.append((limit,backlog))
        return {'completed':0,'failed':0}
    monkeypatch.setattr(worker,'process_pending',process)
    stop=StopAfterPolls(2)
    result=worker.run_worker('cognee',object(),'owner','source',tmp_path,
        config=PreparedConfig(),stop=stop,poll_seconds=9,emit=events.append)
    assert calls==[(2,False),(2,False)] and stop.polls==[9,9]
    assert [row['event'] for row in events]==['started','idle','idle']
    assert result['mode']=='bounded_sample'


def test_transient_errors_are_sanitized_and_wait_before_retry(tmp_path,monkeypatch):
    events=[]
    def fail(*args,**kwargs):
        raise OSError('postgresql://password PRIVATE historical sentence /secret/path')
    monkeypatch.setattr(worker,'derive_pending',fail)
    stop=StopAfterPolls(2)
    result=worker.run_worker('knowledge',object(),'owner','source',tmp_path,
                             stop=stop,poll_seconds=8,emit=events.append)
    assert result['batch_errors']==2 and stop.polls==[8,8]
    assert events[1]['error_code']=='LOCAL_IO_UNAVAILABLE'
    assert all(secret not in json.dumps(events) for secret in ('password','PRIVATE','/secret'))


def test_productive_work_drains_then_waits_on_idle_and_failures(tmp_path,monkeypatch):
    batches=iter([{'completed':2,'failed':0},{'completed':1,'failed':0},
                  {'completed':0,'failed':0},{'completed':0,'failed':1},
                  {'completed':1,'failed':1}])
    monkeypatch.setattr(worker,'derive_pending',lambda *a,**k:next(batches))
    stop=StopAfterPolls(5)
    result=worker.run_worker('knowledge',object(),'owner','source',tmp_path,
        batch_size=2,poll_seconds=11,stop=stop,emit=lambda event:None)
    assert stop.polls==[0,0,11,11,11]
    assert result['completed']==4 and result['failed']==2 and result['batch_errors']==0


def test_operator_pacing_applies_only_to_productive_batches(tmp_path,monkeypatch):
    batches=iter([{'completed':1,'failed':0},{'completed':0,'failed':0}])
    monkeypatch.setattr(worker,'process_pending',lambda *a,**k:next(batches))
    stop=StopAfterPolls(2)
    result=worker.run_worker('cognee',object(),'owner','source',tmp_path,
        config=PreparedConfig(),work_pause_seconds=3,poll_seconds=19,
        stop=stop,emit=lambda event:None)
    assert stop.polls==[3,19] and result['completed']==1


def test_shutdown_after_productive_batch_cannot_claim_another(tmp_path,monkeypatch):
    stop=threading.Event()
    calls=[]
    def complete(*args,**kwargs):
        calls.append('claimed')
        stop.set()
        return {'completed':1,'failed':0}
    monkeypatch.setattr(worker,'derive_pending',complete)
    result=worker.run_worker('knowledge',object(),'owner','source',tmp_path,
        stop=stop,emit=lambda event:None)
    assert calls==['claimed'] and result['status']=='stopped'


def test_sigterm_unwinds_active_batch_and_restores_signal_handler(tmp_path,monkeypatch):
    calls,events=[],[]
    def interrupted(*args,**kwargs):
        try:
            calls.append('claimed')
            signal.raise_signal(signal.SIGTERM)
        finally:
            calls.append('released')
    monkeypatch.setattr(worker,'derive_pending',interrupted)
    previous=signal.getsignal(signal.SIGTERM)
    stop=threading.Event()
    with worker._shutdown_signals(stop):
        result=worker.run_worker('knowledge',object(),'owner','source',tmp_path,
                                 stop=stop,emit=events.append)
    assert calls==['claimed','released'] and result['status']=='stopped'
    assert stop.is_set() and signal.getsignal(signal.SIGTERM)==previous


@pytest.mark.parametrize('value,expected',[('0',False),('1',True)])
def test_explicit_backlog_environment_toggle(value,expected,monkeypatch):
    monkeypatch.setenv('ASTER_ARCHIVE_COGNEE_ENABLE_BACKLOG',value)
    assert worker._backlog_enabled() is expected


def test_invalid_backlog_setting_fails_startup_without_job_call(tmp_path,monkeypatch,capsys):
    calls=[]
    monkeypatch.setenv('ASTER_ARCHIVE_COGNEE_ENABLE_BACKLOG','yes PRIVATE')
    monkeypatch.setattr(worker,'run_worker',lambda *a,**k:calls.append(1))
    result=worker.main(['cognee','--owner','owner','--source','source','--archive-root',str(tmp_path)])
    assert result==1 and calls==[]
    output=capsys.readouterr()
    assert 'PRIVATE' not in output.err
    assert json.loads(output.err)['error_code']=='INVALID_CONFIGURATION_OR_RESULT'


def test_cli_backlog_flag_reaches_component_worker(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setenv('ASTER_ARCHIVE_COGNEE_ENABLE_BACKLOG','0')
    monkeypatch.setattr(worker,'PostgresArchive',lambda *a:object())
    def run(*args,**kwargs):
        calls.append(kwargs)
        return {'status':'batch_complete'}
    monkeypatch.setattr(worker,'run_worker',run)
    assert worker.main(['cognee','--owner','owner','--source','source','--archive-root',str(tmp_path),
                        '--once','--enable-backlog','--work-pause-seconds','2'])==0
    assert calls[0]['backlog'] is True and calls[0]['once'] is True
    assert calls[0]['work_pause_seconds']==2


def test_cli_work_pause_environment_is_cognee_only_and_flag_overrides(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setenv('ASTER_ARCHIVE_COGNEE_ENABLE_BACKLOG','0')
    monkeypatch.setenv('ASTER_ARCHIVE_COGNEE_WORK_PAUSE_SECONDS','7')
    monkeypatch.setattr(worker,'PostgresArchive',lambda *a:object())
    monkeypatch.setattr(worker,'run_worker',lambda *a,**k:(calls.append(k) or {'status':'batch_complete'}))
    arguments=['--owner','owner','--source','source','--archive-root',str(tmp_path),'--once']
    assert worker.main(['cognee',*arguments])==0
    assert worker.main(['knowledge',*arguments])==0
    assert worker.main(['cognee',*arguments,'--work-pause-seconds','2'])==0
    assert [call['work_pause_seconds'] for call in calls]==[7,0,2]


def test_invalid_work_pause_environment_is_sanitized_before_job_call(tmp_path,monkeypatch,capsys):
    calls=[]
    monkeypatch.setenv('ASTER_ARCHIVE_COGNEE_ENABLE_BACKLOG','0')
    monkeypatch.setenv('ASTER_ARCHIVE_COGNEE_WORK_PAUSE_SECONDS','PRIVATE invalid value')
    monkeypatch.setattr(worker,'run_worker',lambda *a,**k:calls.append(1))
    result=worker.main(['cognee','--owner','owner','--source','source','--archive-root',str(tmp_path)])
    assert result==1 and calls==[]
    output=capsys.readouterr()
    assert 'PRIVATE' not in output.err
    assert json.loads(output.err)['error_code']=='INVALID_CONFIGURATION_OR_RESULT'


DSN=os.environ.get('ASTER_ARCHIVE_TEST_DSN')


@pytest.mark.skipif(not DSN,reason='disposable PostgreSQL required')
def test_worker_restart_resumes_native_knowledge_jobs(tmp_path):
    from aster_archive.ingest import import_path
    from aster_archive.knowledge import KnowledgeStore
    from aster_archive.store import PostgresArchive
    store=PostgresArchive(DSN);store.migrate();KnowledgeStore(store).migrate()
    owner='enrichment-worker-'+uuid.uuid4().hex
    rows=[{'id':str(i),'title':'Synthetic worker restart','current_node':'u','mapping':{
        'u':{'parent':None,'children':[],'message':{'id':'u','author':{'role':'user'},
            'content':{'content_type':'text','parts':['Use PostgreSQL for the archive.']}}}}} for i in range(2)]
    export=tmp_path/'export.json';export.write_text(json.dumps(rows))
    root=tmp_path/'archive';import_path(store,export,root,owner,'source')
    results=[worker.run_worker('knowledge',store,owner,'source',root,batch_size=1,
                               once=True,emit=lambda event:None) for _ in range(3)]
    assert [row['completed'] for row in results]==[1,1,0]
    with store.connection(readonly=True) as conn:
        jobs=conn.execute('''SELECT j.status,j.attempts FROM aster_archive.knowledge_jobs j
            JOIN aster_archive.conversation_revisions r USING(revision_id)
            WHERE r.owner_id=%s''',(owner,)).fetchall()
    assert len(jobs)==2 and all(row=={'status':'complete','attempts':1} for row in jobs)
