#!/usr/bin/env python3
"""Root-only, read-only Drive export intake; publish complete ZIPs atomically."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile

GIB = 1024**3
RESERVE = 10 * GIB
MAX_LIST_BYTES = 2 * 1024**2
HEX = re.compile(r'[0-9a-f]{64}')
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{10,200}')
RCLONE = '/home/tony/.local/bin/rclone'
RCLONE_CONFIG = '/etc/aster-archive-backup/rclone.conf'


class IntakeError(Exception):
    pass


def fail(code):
    raise IntakeError(code)


def private_file(path):
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or path.resolve() != path or not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0 or info.st_mode & 0o077):
        fail('PRIVATE_FILE_REQUIRED')
    return path


def directory(path, *, private=True):
    path = Path(path)
    if not path.is_absolute() or path.resolve() != path:
        fail('UNSAFE_DIRECTORY')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    forbidden = 0o077 if private else 0o022
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & forbidden:
        fail('UNSAFE_DIRECTORY_PERMISSIONS')
    return path


def load_config(path):
    if os.geteuid() != 0:
        fail('ROOT_REQUIRED')
    value = json.loads(private_file(path).read_text())
    if value.get('expected_hostname') != socket.gethostname():
        fail('HOST_MISMATCH')
    remote = value.get('drive_remote', '')
    if (not isinstance(remote, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 _.-]{0,80}', remote)
            or 'REPLACE' in remote):
        fail('INVALID_REMOTE')
    if not IDENTIFIER.fullmatch(value.get('drive_root_folder_id', '')) or 'REPLACE' in value['drive_root_folder_id']:
        fail('INVALID_SOURCE_FOLDER')
    # Fixed, already qualified executable and root-private credential copy.
    private_file(RCLONE_CONFIG)
    if not Path(RCLONE).is_file() or not os.access(RCLONE, os.X_OK):
        fail('RCLONE_UNAVAILABLE')
    if not Path('/usr/bin/setfacl').is_file():
        fail('ACL_TOOL_UNAVAILABLE')
    for key in ('state_dir', 'staging_dir', 'inbox_dir'):
        value[key] = directory(value[key], private=key != 'inbox_dir')
    paths = [value[key] for key in ('state_dir', 'staging_dir', 'inbox_dir')]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for i,a in enumerate(paths) for b in paths[i+1:]):
        fail('DIRECTORIES_MUST_BE_SEPARATE')
    if value['staging_dir'].stat().st_dev != value['inbox_dir'].stat().st_dev:
        fail('STAGING_AND_INBOX_MUST_SHARE_FILESYSTEM')
    for key, default, maximum in (('max_listing_entries', 500, 2000), ('max_files_per_run', 2, 10),
                                  ('max_file_bytes', 8*GIB, 20*GIB)):
        value.setdefault(key, default)
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            fail('INVALID_BUDGET')
    baselines = value.get('baseline', [])
    if not isinstance(baselines, list) or len(baselines) > 100:
        fail('INVALID_BASELINE')
    seen = set()
    for item in baselines:
        if (not isinstance(item, dict) or not IDENTIFIER.fullmatch(item.get('file_id', ''))
                or not HEX.fullmatch(item.get('sha256', '')) or item['file_id'] in seen
                or type(item.get('size')) is not int or not 1 <= item['size'] <= value['max_file_bytes']):
            fail('INVALID_BASELINE')
        if item.get('modified') is not None:
            item['modified'] = modified(item['modified'])
        seen.add(item['file_id'])
    value['baseline'] = baselines
    return value


def modified(value):
    if not isinstance(value, str) or len(value) > 64:
        fail('INVALID_MODIFICATION_TIME')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            fail('INVALID_MODIFICATION_TIME')
    except ValueError:
        fail('INVALID_MODIFICATION_TIME')
    return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')


def parse_listing(raw, cfg):
    if len(raw) > MAX_LIST_BYTES:
        fail('LISTING_BUDGET_EXCEEDED')
    rows = json.loads(raw)
    if not isinstance(rows, list) or len(rows) > cfg['max_listing_entries']:
        fail('LISTING_BUDGET_EXCEEDED')
    names, ids, selected = set(), set(), []
    for row in rows:
        if not isinstance(row, dict):
            fail('INVALID_LISTING')
        name = row.get('Path')
        if (not isinstance(name, str) or not name or len(name.encode('utf-8')) > 240
                or name != row.get('Name') or name in ('.', '..') or name.startswith('.')
                or any(c in name for c in '/\\:') or any(unicodedata.category(c).startswith('C') for c in name)):
            fail('UNSAFE_REMOTE_NAME')
        key = unicodedata.normalize('NFC', name).casefold()
        if key in names:
            fail('AMBIGUOUS_REMOTE_PATH')
        names.add(key)
        if row.get('IsDir') is True:
            continue
        if not name.lower().endswith('.zip') or not any(word in name.lower() for word in ('chatgpt', 'export')):
            continue
        if (row.get('IsDir') is not False or row.get('IsLink') or row.get('IsSymlink')
                or row.get('MimeType') in ('application/vnd.google-apps.shortcut', 'inode/symlink')):
            fail('REMOTE_LINK_REJECTED')
        ident, size = row.get('ID'), row.get('Size')
        if not isinstance(ident, str) or not IDENTIFIER.fullmatch(ident) or ident in ids:
            fail('AMBIGUOUS_FILE_ID')
        if type(size) is not int or not 1 <= size <= cfg['max_file_bytes']:
            fail('FILE_SIZE_BUDGET_EXCEEDED')
        hashes = row.get('Hashes') or {}
        checksum = hashes.get('SHA-256', '')
        if checksum and (not isinstance(checksum, str) or not HEX.fullmatch(checksum)):
            fail('INVALID_REMOTE_HASH')
        ids.add(ident)
        selected.append({'file_id':ident, 'path':name, 'size':size,
                         'modified':modified(row.get('ModTime')), 'remote_sha256':checksum})
    return sorted(selected, key=lambda item:(item['modified'],item['file_id']))


def version_key(item, folder):
    body = {key:item[key] for key in ('file_id','size','modified','remote_sha256')}
    body['folder'] = folder
    return hashlib.sha256(json.dumps(body,sort_keys=True).encode()).hexdigest()


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle,'sha256').hexdigest()


def fsync_directory(path):
    fd = os.open(path,os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class Intake:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state, self.staging, self.inbox = (Path(cfg[key]) for key in ('state_dir','staging_dir','inbox_dir'))
        db_path = self.state/'receipts.sqlite3'
        if db_path.exists() or db_path.is_symlink():
            private_file(db_path)
        self.db = sqlite3.connect(db_path)
        db_path.chmod(0o600)
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''CREATE TABLE IF NOT EXISTS versions (
            version TEXT PRIMARY KEY, file_id TEXT NOT NULL, source_path TEXT NOT NULL,
            modified TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
            outcome TEXT NOT NULL, recorded_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contents (sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL,
            outcome TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), value TEXT NOT NULL);''')
        identity = json.dumps({key:str(cfg[key]) for key in ('drive_remote','drive_root_folder_id','inbox_dir')},sort_keys=True)
        previous = self.db.execute('SELECT value FROM identity WHERE singleton=1').fetchone()
        if previous and previous[0] != identity:
            fail('INTAKE_IDENTITY_CHANGED')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO identity VALUES (1,?)',(identity,))
        self.env = {key:os.environ[key] for key in ('PATH','LANG','LC_ALL') if key in os.environ}
        self.env.update(RCLONE_CONFIG=RCLONE_CONFIG,
                        RCLONE_DRIVE_ROOT_FOLDER_ID=cfg['drive_root_folder_id'],GOMAXPROCS='2')

    def run(self, args, *, capture=False, output_limit=MAX_LIST_BYTES, timeout=180):
        def limits():
            resource.setrlimit(resource.RLIMIT_FSIZE,(output_limit,output_limit))
        with tempfile.TemporaryFile(dir=self.staging) as output:
            process = subprocess.Popen([RCLONE,*args,'--config',RCLONE_CONFIG,
                '--drive-skip-shortcuts','--retries','1','--low-level-retries','2',
                '--contimeout','10s','--timeout','2m','--stats','0','--log-level','ERROR'],
                stdin=subprocess.DEVNULL,stdout=output if capture else subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,env=self.env,start_new_session=True,preexec_fn=limits)
            try:
                process.wait(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(process.pid,signal.SIGTERM)
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL); process.wait()
                except ProcessLookupError:
                    pass
                raise
            if process.returncode:
                fail('RCLONE_OPERATION_FAILED')
            output.seek(0)
            data = output.read(MAX_LIST_BYTES+1) if capture else b''
            if len(data) > MAX_LIST_BYTES:
                fail('LISTING_BUDGET_EXCEEDED')
            return data

    def listing(self):
        return parse_listing(self.run(['lsjson',self.cfg['drive_remote']+':','--max-depth','1',
            '--hash-type','SHA-256','--disable','ListR'],capture=True),self.cfg)

    def copy(self, item, destination):
        self.run(['copyto',self.cfg['drive_remote']+':'+item['path'],str(destination),
            '--inplace','--ignore-times','--transfers','1','--checkers','1','--buffer-size','8M',
            '--multi-thread-streams','0','--bwlimit','16M'],output_limit=max(item['size'],1024**2),timeout=3600)

    def acl(self, path, *, directory=False):
        result = subprocess.run(['/usr/bin/setfacl','-m','u:10001:r-x' if directory else 'u:10001:r--',str(path)],
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        if result.returncode:
            fail('READER_ACL_FAILED')

    def record(self, item, checksum, outcome):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO contents VALUES (?,?,?)',(checksum,item['size'],outcome))
            self.db.execute('INSERT INTO versions VALUES (?,?,?,?,?,?,?,?)',(
                version_key(item,self.cfg['drive_root_folder_id']),item['file_id'],item['path'],
                item['modified'],item['size'],checksum,outcome,datetime.now(timezone.utc).isoformat()))

    def baseline(self, item):
        for known in self.cfg.get('baseline',[]):
            if known['file_id'] != item['file_id'] or known['size'] != item['size']:
                continue
            if item['remote_sha256']:
                return known['sha256'] if item['remote_sha256']==known['sha256'] else None
            if known.get('modified'):
                return known['sha256'] if known['modified']==item['modified'] else None
            fail('BASELINE_VERSION_PROOF_REQUIRED')
        return None

    def deliver(self, item):
        key = version_key(item,self.cfg['drive_root_folder_id'])
        target = self.staging/(key+'.partial')
        if target.exists() or target.is_symlink():
            info=target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
                fail('UNSAFE_STAGING_FILE')
            target.unlink()
        if shutil.disk_usage(self.staging).free < RESERVE+max(item['size'],1024**2):
            fail('TEN_GIB_STORAGE_RESERVE_REQUIRED')
        fd = os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        os.close(fd)
        try:
            self.copy(item,target)
            private_file(target)
            if target.stat().st_size != item['size']:
                fail('DOWNLOAD_SIZE_MISMATCH')
            if shutil.disk_usage(self.staging).free < RESERVE:
                fail('TEN_GIB_STORAGE_RESERVE_REQUIRED')
            refreshed = [row for row in self.listing() if row['path']==item['path']]
            if refreshed != [item]:
                fail('REMOTE_VERSION_CHANGED')
            checksum = digest(target)
            if item['remote_sha256'] and checksum != item['remote_sha256']:
                fail('DOWNLOAD_HASH_MISMATCH')
            if not zipfile.is_zipfile(target):
                fail('INVALID_ZIP')
            if self.db.execute('SELECT 1 FROM contents WHERE sha256=? AND size=?',(checksum,item['size'])).fetchone():
                self.record(item,checksum,'content_duplicate')
                return 'deduplicated'
            final = self.inbox/('drive-export-'+checksum+'.zip')
            self.acl(target)
            with target.open('rb') as handle:
                os.fsync(handle.fileno())
            try:
                # Link creates the final complete name atomically and never
                # overwrites a previous delivery. Both directories share st_dev.
                os.link(target,final,follow_symlinks=False)
            except FileExistsError:
                info=final.lstat()
                if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022
                        or info.st_size!=item['size'] or digest(final)!=checksum):
                    fail('INBOX_COLLISION')
                self.acl(final)
            fsync_directory(self.inbox)
            self.record(item,checksum,'delivered')
            return 'delivered'
        finally:
            target.unlink(missing_ok=True)

    def intake(self):
        # The external flock excludes other intake processes. Recover only our
        # exact private partial names, including versions since replaced in Drive.
        stale=list(self.staging.iterdir())
        if len(stale)>1000:
            fail('STAGING_ENTRY_BUDGET_EXCEEDED')
        for path in stale:
            info=path.lstat()
            if (not re.fullmatch(r'[0-9a-f]{64}\.partial',path.name)
                    or not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022):
                fail('UNMANAGED_STAGING_ENTRY')
            path.unlink()
        self.acl(self.inbox,directory=True)
        rows = self.listing()
        result={'status':'complete','eligible':len(rows),'delivered':0,'deduplicated':0,
                'baseline_seeded':0,'skipped_versions':0,'deferred':0}
        attempts=0
        for item in rows:
            key=version_key(item,self.cfg['drive_root_folder_id'])
            if self.db.execute('SELECT 1 FROM versions WHERE version=?',(key,)).fetchone():
                result['skipped_versions']+=1; continue
            checksum=self.baseline(item)
            if checksum:
                self.record(item,checksum,'already_imported_baseline')
                result['baseline_seeded']+=1; continue
            if attempts>=self.cfg['max_files_per_run']:
                result['deferred']+=1; continue
            attempts+=1
            result[self.deliver(item)]+=1
        return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True,type=Path)
    args=parser.parse_args(argv)
    os.umask(0o077)
    def stop(signum,frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,stop)
    try:
        cfg=load_config(args.config)
        lock_path=cfg['state_dir']/'intake.lock'
        fd=os.open(lock_path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'r+') as lock:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({'status':'already_running'})); return 0
            app=Intake(cfg)
            try:
                result=app.intake()
            finally:
                app.db.close()
        print(json.dumps(result)); return 0
    except KeyboardInterrupt:
        print(json.dumps({'status':'interrupted'})); return 0
    except Exception as error:
        code=str(error) if isinstance(error,IntakeError) else 'INTAKE_OPERATION_FAILED'
        print(json.dumps({'status':'failed','error_code':code}),file=sys.stderr); return 1


if __name__=='__main__':
    raise SystemExit(main())
