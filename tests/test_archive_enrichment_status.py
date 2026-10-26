"""A quiet queue is not complete coverage; stalls require a persistent baseline."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('archive_enrichment_status',
    ROOT/'scripts/archive-enrichment-status.py')
status=importlib.util.module_from_spec(spec)
spec.loader.exec_module(status)


def snapshot():
    # Three text passages, only two from ordinary user/assistant messages.
    return {'schema_version':1,'database_schema_version':1,
        'observed_at':'2026-01-01T00:00:00+00:00','corpus_fingerprint':'a'*64,
        'corpus':{'conversations':1,'ambiguous_current_revisions':0,'missing_current_revisions':0},
        'imports':{'pending':0,'running':0,'complete':1,'complete_with_errors':0,'failed':0},
        'embeddings':{'generation_id':'b'*64,'current_passages':3,'embedded_passages':3,
            'unique_texts':2,'embedded_unique_texts':2,'pending_unique_texts':0,
            'running_unique_texts':0,'failed_unique_texts':0,'unseeded_unique_texts':0},
        'cognee':{'pipeline_key':'synthetic-pipeline','eligible_passages':2,
            'completed_passages':2,'pending_passages':0,'running_passages':0,
            'failed_passages':0,'unseeded_passages':0}}


def pending_snapshot():
    value=snapshot()
    value['embeddings'].update(embedded_passages=1,embedded_unique_texts=1,unseeded_unique_texts=1)
    value['cognee'].update(completed_passages=1,unseeded_passages=1)
    return value


def test_complete_requires_correct_per_component_denominators():
    result=status.classify(snapshot())
    assert result['complete']
    assert result['embeddings']['complete'] and result['cognee']['complete']
    assert result['cognee']['eligible_passages'] != result['embeddings']['current_passages']


@pytest.mark.parametrize('kind',['unseeded','pending','running','failed'])
def test_remaining_cognee_work_cannot_be_declared_complete(kind):
    value=snapshot()
    value['cognee']['completed_passages']=1
    value['cognee'][kind+'_passages']=1
    result=status.classify(value)
    assert not result['complete'] and not result['cognee']['complete']
    assert result['cognee']['remaining_passages']==1


def test_unseeded_embedding_hash_prevents_false_quiet_queue_completion():
    value=snapshot()
    value['embeddings'].update(embedded_passages=1,embedded_unique_texts=1,unseeded_unique_texts=1)
    result=status.classify(value)
    assert not result['complete'] and result['embeddings']['remaining_passages']==2


@pytest.mark.parametrize('kind',['pending','running','complete_with_errors','failed'])
def test_unfinished_imports_prevent_completion(kind):
    value=snapshot();value['imports'][kind]=1
    result=status.classify(value)
    assert not result['imports_complete'] and not result['complete']


@pytest.mark.parametrize('kind',['ambiguous_current_revisions','missing_current_revisions'])
def test_unresolved_current_revision_prevents_completion(kind):
    value=snapshot();value['corpus'][kind]=1
    assert not status.classify(value)['complete']


def test_empty_corpus_or_no_completed_import_never_passes():
    value=snapshot();value['imports']['complete']=0
    assert not status.classify(value)['complete']
    value=snapshot();value['corpus']['conversations']=0
    for key in status.EMBEDDING_COUNTS:value['embeddings'][key]=0
    for key in status.COGNEE_COUNTS:value['cognee'][key]=0
    assert not status.classify(value)['complete']


@pytest.mark.parametrize('change',[
    lambda v:v['cognee'].update(completed_passages=3),
    lambda v:v['embeddings'].update(embedded_passages=True),
    lambda v:v['embeddings'].update(unseeded_unique_texts=-1),
    lambda v:v.update(private_text='must never be printed'),
    lambda v:v['corpus'].update(ambiguous_current_revisions=2),
])
def test_malformed_or_unexpected_fields_are_rejected(change):
    value=snapshot();change(value)
    with pytest.raises(status.ProbeError):status.classify(value)


def test_stall_uses_cumulative_time_and_resets_independently_after_progress():
    first=status.progress(status.classify(pending_snapshot()))
    assert not first['stalled']
    next_value=pending_snapshot();next_value['observed_at']='2026-01-01T01:00:00+00:00'
    second=status.progress(status.classify(next_value),first)
    assert not second['stalled']
    next_value=pending_snapshot();next_value['observed_at']='2026-01-01T02:00:00+00:00'
    next_value['embeddings'].update(embedded_passages=3,embedded_unique_texts=2,unseeded_unique_texts=0)
    third=status.progress(status.classify(next_value),second)
    assert third['stalled'] and third['progress']['cognee']['stalled']
    assert not third['progress']['embeddings']['stalled']
    assert third['progress']['cognee']['last_progress_at']==first['observed_at']
    assert third['progress']['embeddings']['last_progress_at']==third['observed_at']


def test_new_corpus_resets_progress_baseline_even_with_lower_counts():
    old=status.progress(status.classify(snapshot()))
    value=pending_snapshot();value['corpus_fingerprint']='c'*64
    value['observed_at']='2026-01-02T00:00:00+00:00'
    result=status.progress(status.classify(value),old)
    assert not result['stalled']
    assert all(row['last_progress_at']==value['observed_at'] for row in result['progress'].values())


def test_same_corpus_progress_regression_fails_closed():
    old=status.progress(status.classify(snapshot()))
    with pytest.raises(status.ProbeError,match='PROGRESS_REGRESSED'):
        status.progress(status.classify(pending_snapshot()),old)


def test_older_snapshot_cannot_replace_newer_checkpoint_even_if_scope_changed():
    value=snapshot();value['observed_at']='2026-01-02T00:00:00+00:00'
    old=status.progress(status.classify(value))
    value=pending_snapshot();value['corpus_fingerprint']='c'*64
    with pytest.raises(status.ProbeError,match='OBSERVATION_TIME_REGRESSED'):
        status.progress(status.classify(value),old)


def test_complete_corpus_never_stalls_after_long_idle():
    old=status.progress(status.classify(snapshot()))
    value=snapshot();value['observed_at']='2026-01-03T00:00:00+00:00'
    assert not status.progress(status.classify(value),old)['stalled']


def test_probe_passes_identities_as_arguments_without_shell_or_model_calls(monkeypatch):
    compile(status.INNER,'reader-probe','exec')
    calls=[]
    def run(argv,**kwargs):
        calls.append((argv,kwargs))
        return subprocess.CompletedProcess(argv,0,json.dumps(snapshot()).encode())
    monkeypatch.setattr(status.subprocess,'run',run)
    assert status.probe('owner; literal','source $(literal)','aster-archive-mcp-1')['complete']
    argv,options=calls[0]
    assert argv==['docker','exec','-i','aster-archive-mcp-1','python','-','owner; literal','source $(literal)']
    assert options['input']==status.INNER.encode() and options['stderr']==subprocess.DEVNULL
    assert 'shell' not in options
    with pytest.raises(status.ProbeError):status.probe('owner','source','unsafe;container')
    assert len(calls)==1


def test_failure_does_not_echo_database_or_command_details(monkeypatch,capsys):
    def fail(*args,**kwargs):raise RuntimeError('postgresql://SECRET PRIVATE HISTORICAL TEXT')
    monkeypatch.setattr(status,'probe',fail)
    assert status.main(['--owner','owner','--source','source'])==1
    output=capsys.readouterr().out
    assert json.loads(output)['error_code']=='STATUS_PROBE_FAILED'
    assert 'SECRET' not in output and 'HISTORICAL' not in output


@pytest.mark.skipif(os.geteuid()!=0,reason='root-private state ownership test')
def test_private_checkpoints_are_atomic_and_reject_symlink_or_public_state(tmp_path):
    directory=tmp_path/'private'
    result=status.save_progress(status.classify(pending_snapshot()),directory)
    assert json.loads((directory/'latest.json').read_text())==result
    assert len((directory/'history.ndjson').read_text().splitlines())==1
    assert directory.stat().st_mode & 0o077==0
    assert (directory/'latest.json').stat().st_mode & 0o077==0
    with (directory/'status.lock').open('r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            status.save_progress(status.classify(snapshot()),directory)
    assert len((directory/'history.ndjson').read_text().splitlines())==1
    (directory/'latest.json').unlink()
    target=tmp_path/'target';target.write_text('must remain unchanged')
    (directory/'latest.json').symlink_to(target)
    with pytest.raises(OSError):status.save_progress(status.classify(snapshot()),directory)
    assert target.read_text()=='must remain unchanged'
    directory.chmod(0o755)
    with pytest.raises(status.ProbeError,match='UNSAFE_STATE_DIRECTORY'):
        status.save_progress(status.classify(snapshot()),directory)
