"""Lossless completed-revision storage, interrupted transitions and hostile archives."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tarfile

import pytest

from aster_archive import cognee_storage as storage
from aster_archive.storage import ArchiveStorageError


@pytest.fixture
def revision(tmp_path, monkeypatch):
    monkeypatch.setenv('ASTER_ARCHIVE_MIN_FREE_BYTES', '1')
    root = tmp_path / ('a' * 64)
    (root / 'data' / 'document').mkdir(parents=True)
    (root / 'system' / 'databases' / 'dataset.lance.db').mkdir(parents=True)
    (root / 'cache' / 'empty').mkdir(parents=True)
    (root / 'receipts').mkdir()
    files = {
        'data/document/passage.txt': 'Synthetic passage with İstanbul and café.\n'.encode(),
        'system/databases/archive.sqlite': bytes(range(256)) * 64,
        'system/databases/archive.sqlite-wal': b'WAL checkpoint still pending\0' * 137,
        'system/databases/archive.sqlite-shm': bytes(reversed(range(256))) * 16,
        'system/databases/dataset.lbug': b'GRAPH\0' + bytes(180_000),
        'system/databases/dataset.lance.db/payload.lance': b'VECTORS\0' * 127,
        'cache/config.json': b'{"offline":true}\n',
    }
    for relative, contents in files.items():
        target = root / relative
        target.write_bytes(contents)
        target.chmod(0o600)
    (root / 'cache' / 'config.json').chmod(0o400)
    (root / 'cache' / 'empty').chmod(0o700)
    (root / 'receipts' / ('b' * 64 + '.json')).write_bytes(b'{"status":"complete"}\n')
    (root / 'local-only.env').write_bytes(b'# Dedicated local runtime.\n')
    for relative in ('receipts/' + 'b' * 64 + '.json', 'local-only.env'):
        (root / relative).chmod(0o600)
    return root


def live_snapshot(root):
    """Compare actual bytes/modes and empty directories, without archive artifacts."""
    result = {}
    for name in ('data', 'system', 'cache'):
        directory = root / name
        if not directory.exists():
            continue
        for target in [directory, *sorted(directory.rglob('*'))]:
            meta = target.lstat()
            relative = target.relative_to(root).as_posix()
            result[relative] = {
                'mode': stat.S_IMODE(meta.st_mode),
                'type': 'directory' if target.is_dir() else 'file',
                'sha256': hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None,
            }
    return result


def retained_snapshot(root):
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
        for p in [root / 'local-only.env', *sorted((root / 'receipts').glob('*.json'))]
    }


def marker(root):
    return json.loads((root / storage.MARKER_NAME).read_text())


def freeze(root):
    return storage.compact_revision(root, eligible_passages=3, completed_passages=3)


def test_completed_revision_roundtrip_preserves_all_bytes_modes_and_sidecars(revision):
    original = live_snapshot(revision)
    retained = retained_snapshot(revision)
    freeze(revision)
    saved = marker(revision)
    assert saved['state'] == 'frozen'
    assert saved['scope_id'] == revision.name
    assert saved['file_count'] == sum(value['type'] == 'file' for value in original.values())
    assert saved['logical_bytes'] > 0
    assert not any((revision / name).exists() for name in ('data', 'system', 'cache'))
    assert retained_snapshot(revision) == retained
    archive = revision / storage.ARCHIVE_NAME
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == saved['archive_sha256']
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE((revision / storage.MARKER_NAME).stat().st_mode) == 0o600
    storage.hydrate_revision(revision)
    assert live_snapshot(revision) == original
    assert retained_snapshot(revision) == retained
    assert marker(revision)['state'] == 'hydrated'
    assert archive.is_file()
    storage.hydrate_revision(revision)
    assert live_snapshot(revision) == original


@pytest.mark.parametrize('eligible,completed', [(0, 0), (3, 2), (3, 4), (-1, -1), (True, True)])
def test_incomplete_or_invalid_revision_cannot_be_compacted(revision, eligible, completed):
    original = live_snapshot(revision)
    with pytest.raises((ValueError, storage.CogneeStorageError)):
        storage.compact_revision(revision, eligible_passages=eligible, completed_passages=completed)
    assert live_snapshot(revision) == original
    assert not (revision / storage.MARKER_NAME).exists()


def test_failed_archive_verification_keeps_live_files(revision, monkeypatch):
    original = live_snapshot(revision)
    def corrupted(*args, **kwargs):
        raise storage.CogneeStorageError('synthetic archive verification failure')
    monkeypatch.setattr(storage, '_verify_archive', corrupted)
    with pytest.raises(storage.CogneeStorageError):
        freeze(revision)
    assert live_snapshot(revision) == original
    assert not (revision / storage.MARKER_NAME).exists()


def test_corrupt_archive_cannot_promote_restored_files(revision):
    retained = retained_snapshot(revision)
    freeze(revision)
    archive = revision / storage.ARCHIVE_NAME
    data = bytearray(archive.read_bytes())
    data[len(data) // 2] ^= 1
    archive.write_bytes(data)
    with pytest.raises(storage.CogneeStorageError):
        storage.hydrate_revision(revision)
    assert not any((revision / name).exists() for name in ('data', 'system', 'cache'))
    assert retained_snapshot(revision) == retained
    assert marker(revision)['state'] == 'frozen'


def test_partial_cleanup_after_committed_marker_is_recoverable(revision, monkeypatch):
    original = live_snapshot(revision)
    retained = retained_snapshot(revision)
    actual_remove = storage._remove_live
    def interrupted(*args, **kwargs):
        shutil.rmtree(revision / 'data')
        raise RuntimeError('simulated interruption during live cleanup')
    monkeypatch.setattr(storage, '_remove_live', interrupted)
    with pytest.raises(Exception):
        freeze(revision)
    assert marker(revision)['state'] == 'frozen'
    assert (revision / storage.ARCHIVE_NAME).is_file()
    assert not (revision / 'data').exists()
    assert (revision / 'system').exists()
    monkeypatch.setattr(storage, '_remove_live', actual_remove)
    storage.hydrate_revision(revision)
    assert live_snapshot(revision) == original
    assert retained_snapshot(revision) == retained


def test_interrupted_hydration_can_resume_without_losing_archive(revision, monkeypatch):
    original = live_snapshot(revision)
    freeze(revision)
    actual_replace = os.replace
    interrupted = []
    def replace(source, destination, *args, **kwargs):
        if Path(destination) == revision / 'system' and not interrupted:
            interrupted.append(True)
            raise RuntimeError('simulated interruption during hydration')
        return actual_replace(source, destination, *args, **kwargs)
    monkeypatch.setattr(storage.os, 'replace', replace)
    with pytest.raises(Exception):
        storage.hydrate_revision(revision)
    assert interrupted
    assert marker(revision)['state'] == 'frozen'
    assert (revision / storage.ARCHIVE_NAME).is_file()
    monkeypatch.setattr(storage.os, 'replace', actual_replace)
    storage.hydrate_revision(revision)
    assert live_snapshot(revision) == original


def test_symlinked_input_is_rejected_without_touching_target(revision, tmp_path):
    outside = tmp_path / 'outside-secret'
    outside.write_bytes(b'must remain outside storage')
    (revision / 'cache' / 'escape').symlink_to(outside)
    with pytest.raises(storage.CogneeStorageError):
        freeze(revision)
    assert outside.read_bytes() == b'must remain outside storage'
    assert (revision / 'data' / 'document' / 'passage.txt').is_file()
    assert not (revision / storage.MARKER_NAME).exists()


def rewrite_archive(root, mutate):
    archive = root / storage.ARCHIVE_NAME
    original = []
    with tarfile.open(archive, 'r:*') as handle:
        for member in handle:
            original.append((member, handle.extractfile(member).read() if member.isfile() else None))
    changed = mutate(original)
    with tarfile.open(archive, 'w:xz', preset=3) as handle:
        for member, contents in changed:
            handle.addfile(member, io.BytesIO(contents) if contents is not None else None)
    saved = marker(root)
    saved['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    (root / storage.MARKER_NAME).write_text(json.dumps(saved))


@pytest.mark.parametrize('kind', ['parent', 'absolute', 'symlink', 'hardlink', 'duplicate'])
def test_hostile_tar_members_are_rejected_even_with_updated_archive_checksum(revision, kind):
    freeze(revision)
    def mutate(entries):
        first_file = next((member, data) for member, data in entries if member.isfile())
        if kind == 'duplicate':
            return entries + [first_file]
        malicious = tarfile.TarInfo('../escape' if kind == 'parent' else '/escape' if kind == 'absolute' else 'cache/link')
        malicious.mode = 0o600
        if kind in ('symlink', 'hardlink'):
            malicious.type = tarfile.SYMTYPE if kind == 'symlink' else tarfile.LNKTYPE
            malicious.linkname = '../receipts/' + 'b' * 64 + '.json'
            payload = None
        else:
            payload = b'escape'
            malicious.size = len(payload)
        return entries + [(malicious, payload)]
    rewrite_archive(revision, mutate)
    with pytest.raises(storage.CogneeStorageError):
        storage.hydrate_revision(revision)
    assert not (revision.parent / 'escape').exists()
    assert marker(revision)['state'] == 'frozen'
    assert not any((revision / name).exists() for name in ('data', 'system', 'cache'))


@pytest.mark.parametrize('operation', ['compact', 'hydrate'])
def test_capacity_refusal_keeps_authoritative_copy(revision, monkeypatch, operation):
    original = live_snapshot(revision)
    if operation == 'hydrate':
        freeze(revision)
    def full(*args, **kwargs):
        raise ArchiveStorageError('synthetic low disk')
    monkeypatch.setattr(storage, 'require_storage_reserve', full)
    with pytest.raises(ArchiveStorageError):
        freeze(revision) if operation == 'compact' else storage.hydrate_revision(revision)
    if operation == 'compact':
        assert live_snapshot(revision) == original
    else:
        assert (revision / storage.ARCHIVE_NAME).is_file()
        assert marker(revision)['state'] == 'frozen'


@pytest.mark.parametrize('bound,value', [('MAX_MEMBERS', 2), ('MAX_LOGICAL_BYTES', 100), ('MAX_FILE_BYTES', 100)])
def test_archive_bounds_fail_before_original_removal(revision, monkeypatch, bound, value):
    original = live_snapshot(revision)
    monkeypatch.setattr(storage, bound, value)
    with pytest.raises(storage.CogneeStorageError):
        freeze(revision)
    assert live_snapshot(revision) == original
    assert not (revision / storage.MARKER_NAME).exists()


def test_unexpected_live_changes_during_frozen_transition_are_not_deleted(revision, monkeypatch):
    actual_remove = storage._remove_live
    def interrupted(*args, **kwargs):
        raise RuntimeError('simulated interruption before cleanup')
    monkeypatch.setattr(storage, '_remove_live', interrupted)
    with pytest.raises(Exception):
        freeze(revision)
    altered = revision / 'data' / 'document' / 'passage.txt'
    altered.write_bytes(b'Unexpected concurrent mutation must survive.')
    monkeypatch.setattr(storage, '_remove_live', actual_remove)
    with pytest.raises(storage.CogneeStorageError):
        storage.hydrate_revision(revision)
    assert altered.read_bytes() == b'Unexpected concurrent mutation must survive.'
    assert (revision / storage.ARCHIVE_NAME).is_file()
    assert marker(revision)['state'] == 'frozen'


def test_oversized_manifest_is_rejected_without_restore(revision, monkeypatch):
    freeze(revision)
    monkeypatch.setattr(storage, 'MAX_MANIFEST_BYTES', 64)
    with pytest.raises(storage.CogneeStorageError):
        storage.hydrate_revision(revision)
    assert marker(revision)['state'] == 'frozen'
    assert not any((revision / name).exists() for name in ('data', 'system', 'cache'))


def test_decoder_memory_limit_is_enforced_before_restore_promotion(revision, monkeypatch):
    freeze(revision)
    # The production archive's LZMA dictionary exceeds this synthetic limit.
    # Exercise the real decoder guard without allocating a preset-9 encoder.
    monkeypatch.setattr(storage, 'MAX_LZMA_MEMORY_BYTES', 1024 * 1024)
    with pytest.raises(storage.CogneeStorageError):
        storage.hydrate_revision(revision)
    assert marker(revision)['state'] == 'frozen'
    assert (revision / storage.ARCHIVE_NAME).is_file()
    assert not any((revision / name).exists() for name in ('data', 'system', 'cache'))


def test_truncated_xz_trailer_is_rejected_even_when_archive_checksum_matches(revision):
    freeze(revision)
    archive = revision / storage.ARCHIVE_NAME
    archive.write_bytes(archive.read_bytes()[:-8])
    saved = marker(revision)
    saved['archive_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
    (revision / storage.MARKER_NAME).write_text(json.dumps(saved))
    with pytest.raises(storage.CogneeStorageError):
        storage.hydrate_revision(revision)
    assert marker(revision)['state'] == 'frozen'
    assert not any((revision / name).exists() for name in ('data', 'system', 'cache'))


def test_exact_receipt_replay_does_not_hydrate_or_launch_a_child(revision, tmp_path, monkeypatch):
    import sys
    from aster_archive import cognee_bridge as bridge
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'manifest.json').write_text(json.dumps(bridge.verified_model_manifest()))
    config = bridge.CogneeConfig(Path(sys.executable), tmp_path / 'cognee', models)
    row = {'owner_id': 'synthetic-owner', 'source_id': 'synthetic-source',
           'revision_id': 'c' * 64, 'passage_id': 'b' * 64, 'role': 'user',
           'text': 'Synthetic source with exact completed receipt.'}
    target = config.root / 'revisions' / bridge.stable_id(
        row['owner_id'], row['source_id'], row['revision_id'], bridge.PIPELINE_KEY)
    target.parent.mkdir(parents=True)
    revision.rename(target)
    receipt = {'status': 'complete', 'summaries': ['Synthetic extraction.'],
               'source_sha256': hashlib.sha256(row['text'].encode()).hexdigest(),
               'manifest_sha256': hashlib.sha256((models / 'manifest.json').read_bytes()).hexdigest(),
               'runtime': {'execution_device': 'cuda', 'runner_pid': 12345}}
    (target / 'receipts' / (row['passage_id'] + '.json')).write_text(json.dumps(receipt))
    storage.compact_revision(target, eligible_passages=1, completed_passages=1)
    def forbidden(*args, **kwargs):
        pytest.fail('A completed exact receipt must not hydrate or launch a child')
    monkeypatch.setattr(bridge, 'hydrated_revision', forbidden)
    monkeypatch.setattr(bridge.subprocess, 'Popen', forbidden)
    with bridge.ScopedCogneeSession(config, [row]) as session:
        result = session.derive(row)
    assert result['reused_completed_receipt'] is True
    assert result['summaries'] == receipt['summaries']
    assert result['runtime'] == receipt['runtime']
    result = bridge.process_passage(config, row)
    assert result['reused_completed_receipt'] is True
    assert result['summaries'] == receipt['summaries']
    assert marker(target)['state'] == 'frozen'
    assert not any((target / name).exists() for name in ('data', 'system', 'cache'))


def test_persistent_writer_barrier_blocks_storage_changes_and_preserves_both_copies(revision):
    original = live_snapshot(revision)
    freeze(revision)
    storage.hydrate_revision(revision)
    archive_before = (revision / storage.ARCHIVE_NAME).read_bytes()
    storage.block_unquiescent_writer(revision, 123456)
    barrier = revision / storage.WRITER_BLOCK_NAME
    assert json.loads(barrier.read_text())['pid_in_writer_namespace'] == 123456
    assert stat.S_IMODE(barrier.stat().st_mode) == 0o600
    for operation in (lambda: freeze(revision), lambda: storage.hydrate_revision(revision)):
        with pytest.raises(storage.CogneeStorageError, match='quiescence'):
            operation()
        assert live_snapshot(revision) == original
        assert (revision / storage.ARCHIVE_NAME).read_bytes() == archive_before
    assert barrier.is_file()
    assert marker(revision)['state'] == 'hydrated'


def test_quiescence_permission_error_persists_barrier_without_retrying_writes(revision, monkeypatch):
    from types import SimpleNamespace
    from aster_archive import cognee_bridge as bridge
    def denied(*args, **kwargs):
        raise PermissionError('synthetic process-group permission failure')
    monkeypatch.setattr(bridge.os, 'killpg', denied)
    with pytest.raises(bridge.CogneeRuntimeUnavailable, match='NOT_QUIESCENT'):
        bridge._quiesce_process_group(SimpleNamespace(pid=123456), revision)
    saved = json.loads((revision / storage.WRITER_BLOCK_NAME).read_text())
    assert saved['reason'] == 'child_group_not_quiescent'
    assert saved['pid_in_writer_namespace'] == 123456
    with pytest.raises(storage.CogneeStorageError, match='quiescence'):
        freeze(revision)


@pytest.mark.parametrize('passage_id', ['../../outside', '../' + 'b' * 64, 'B' * 64])
def test_receipt_fast_path_rejects_invalid_passage_ids_before_file_access(tmp_path, monkeypatch, passage_id):
    import sys
    from aster_archive import cognee_bridge as bridge
    models = tmp_path / 'models'
    models.mkdir()
    (models / 'manifest.json').write_text(json.dumps(bridge.verified_model_manifest()))
    config = bridge.CogneeConfig(Path(sys.executable), tmp_path / 'cognee', models)
    row = {'owner_id': 'synthetic-owner', 'source_id': 'synthetic-source',
           'revision_id': 'c' * 64, 'passage_id': passage_id,
           'role': 'user', 'text': 'Synthetic exact receipt.'}
    outside = tmp_path / 'outside.json'
    outside.write_bytes(b'Outside files must remain untouched.\n')
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid passage IDs must not hydrate or launch a child')
    monkeypatch.setattr(bridge, 'hydrated_revision', forbidden)
    monkeypatch.setattr(bridge.subprocess, 'Popen', forbidden)
    with pytest.raises(ValueError, match='metadata'):
        bridge.process_passage(config, row)
    with bridge.ScopedCogneeSession(config, [row]) as session:
        with pytest.raises(ValueError, match='metadata'):
            session.derive(row)
    assert outside.read_bytes() == b'Outside files must remain untouched.\n'
