import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import zipfile

import pytest

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/archive-drive-intake.py'
spec=importlib.util.spec_from_file_location('drive_intake',SCRIPT)
intake=importlib.util.module_from_spec(spec);spec.loader.exec_module(intake)


def payload(text='[]'):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as archive:
        archive.writestr('conversations.json',text)
    return output.getvalue()


def listing(data, *, name='ChatGPT export.zip', ident='synthetic_file_123', modified='2026-01-01T00:00:00Z', remote_hash=True):
    return {'ID':ident,'Path':name,'Name':name,'IsDir':False,'Size':len(data),'ModTime':modified,
            'MimeType':'application/zip','Hashes':{'SHA-256':hashlib.sha256(data).hexdigest()} if remote_hash else {}}


@pytest.fixture
def cfg(tmp_path,monkeypatch):
    value={'drive_remote':'synthetic','drive_root_folder_id':'synthetic_folder_123',
        'max_listing_entries':100,'max_files_per_run':2,'max_file_bytes':intake.GIB,'baseline':[]}
    for key in ('state_dir','staging_dir','inbox_dir'):
        value[key]=tmp_path/key;value[key].mkdir(mode=0o700)
    monkeypatch.setattr(intake.shutil,'disk_usage',lambda p:shutil._ntuple_diskusage(100*intake.GIB,0,100*intake.GIB))
    return value


class FakeIntake(intake.Intake):
    def __init__(self,cfg,data):
        super().__init__(cfg)
        self.data=data;self.rows=[listing(data)];self.copies=0;self.interrupt=False
        self.changed=False;self.acls=[]

    def listing(self):
        rows=self.rows
        if self.changed and self.copies:
            rows=[dict(rows[0],ModTime='2026-02-01T00:00:00Z')]
        return intake.parse_listing(json.dumps(rows).encode(),self.cfg)

    def copy(self,item,destination):
        self.copies+=1
        destination.write_bytes(self.data[:4] if self.interrupt else self.data)
        if self.interrupt:
            raise OSError('PRIVATE token and corpus must not appear in output')

    def acl(self,path,*,directory=False):
        self.acls.append((path,directory))


@pytest.mark.parametrize('name',['../export.zip','/export.zip','dir/export.zip','dir\\export.zip',
                                 '.export.zip','export\n.zip','export:fake.zip'])
def test_unsafe_remote_names_are_rejected(cfg,name):
    with pytest.raises(intake.IntakeError,match='UNSAFE_REMOTE_NAME'):
        intake.parse_listing(json.dumps([listing(payload(),name=name)]).encode(),cfg)


def test_duplicate_remote_paths_and_shortcuts_fail_closed(cfg):
    row=listing(payload())
    with pytest.raises(intake.IntakeError,match='AMBIGUOUS_REMOTE_PATH'):
        intake.parse_listing(json.dumps([row,dict(row,ID='synthetic_other_123')]).encode(),cfg)
    with pytest.raises(intake.IntakeError,match='REMOTE_LINK_REJECTED'):
        intake.parse_listing(json.dumps([dict(row,MimeType='application/vnd.google-apps.shortcut')]).encode(),cfg)


def test_version_receipt_and_content_dedup_allow_updated_same_drive_id(cfg):
    app=FakeIntake(cfg,payload())
    assert app.intake()['delivered']==1
    assert app.intake()['skipped_versions']==1 and app.copies==1
    app.rows=[listing(app.data,ident='synthetic_other_123')]
    assert app.intake()['deduplicated']==1 and len(list(app.inbox.glob('*.zip')))==1
    app.data=payload('[{"id":"new"}]')
    app.rows=[listing(app.data,modified='2026-02-01T00:00:00Z')]
    assert app.intake()['delivered']==1 and len(list(app.inbox.glob('*.zip')))==2
    assert app.db.execute('SELECT count(*) FROM versions').fetchone()[0]==3


def test_baseline_id_size_modtime_skips_download_without_remote_hash(cfg):
    data=payload()
    cfg['baseline']=[{'file_id':'synthetic_file_123','size':len(data),
        'sha256':hashlib.sha256(data).hexdigest(),'modified':intake.modified('2026-01-01T00:00:00+00:00')}]
    app=FakeIntake(cfg,data);app.rows=[listing(data,remote_hash=False)]
    assert app.intake()['baseline_seeded']==1 and app.copies==0
    assert app.intake()['skipped_versions']==1 and not list(app.inbox.iterdir())
    app.rows=[listing(data,modified='2026-01-02T00:00:00Z',remote_hash=False)]
    assert app.intake()['deduplicated']==1 and app.copies==1


def test_partial_download_never_reaches_inbox_and_retry_succeeds(cfg):
    app=FakeIntake(cfg,payload());app.interrupt=True
    with pytest.raises(OSError):app.intake()
    assert not list(app.inbox.iterdir()) and not list(app.staging.iterdir())
    assert app.db.execute('SELECT count(*) FROM versions').fetchone()[0]==0
    app.interrupt=False
    assert app.intake()['delivered']==1


def test_remote_mutation_during_copy_is_not_delivered(cfg):
    app=FakeIntake(cfg,payload());app.changed=True
    with pytest.raises(intake.IntakeError,match='REMOTE_VERSION_CHANGED'):app.intake()
    assert not list(app.inbox.iterdir()) and not list(app.staging.iterdir())


def test_crash_after_atomic_publish_before_receipt_is_idempotent(cfg,monkeypatch):
    app=FakeIntake(cfg,payload())
    original=app.record
    def crash(*args):raise OSError('synthetic crash')
    monkeypatch.setattr(app,'record',crash)
    with pytest.raises(OSError):app.intake()
    assert len(list(app.inbox.glob('*.zip')))==1
    monkeypatch.setattr(app,'record',original)
    assert app.intake()['delivered']==1
    assert len(list(app.inbox.glob('*.zip')))==1
    assert app.db.execute('SELECT count(*) FROM versions').fetchone()[0]==1


def test_reserve_blocks_before_copy(cfg,monkeypatch):
    app=FakeIntake(cfg,payload())
    monkeypatch.setattr(intake.shutil,'disk_usage',lambda p:shutil._ntuple_diskusage(20*intake.GIB,20*intake.GIB,0))
    with pytest.raises(intake.IntakeError,match='TEN_GIB_STORAGE_RESERVE_REQUIRED'):app.intake()
    assert app.copies==0 and not list(app.inbox.iterdir())


def test_state_cannot_be_reused_for_another_inbox_or_folder(cfg):
    app=FakeIntake(cfg,payload());app.db.close()
    with pytest.raises(intake.IntakeError,match='INTAKE_IDENTITY_CHANGED'):
        FakeIntake(dict(cfg,drive_root_folder_id='another_folder_123'),payload())


def test_raw_remote_or_corpus_errors_are_not_logged(monkeypatch,capsys):
    def fail(*args):raise OSError('PRIVATE token corpus /secret/path')
    monkeypatch.setattr(intake,'load_config',fail)
    monkeypatch.setattr(intake.signal,'signal',lambda *args:None)
    monkeypatch.setattr(intake.os,'umask',lambda value:0o022)
    assert intake.main(['--config','/synthetic/config.json'])==1
    output=capsys.readouterr()
    assert json.loads(output.err)=={'status':'failed','error_code':'INTAKE_OPERATION_FAILED'}
    assert 'PRIVATE' not in output.err
