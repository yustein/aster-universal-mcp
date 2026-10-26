import io
import json
from pathlib import Path
import stat
from types import SimpleNamespace
import zipfile

import pytest

from aster_archive import source
from aster_archive.source import SourceError, SourceLimits, iter_conversations, load_source, prepare_source


def conversation(identity="conversation-a"):
    return {"id": identity, "title": "Aster test", "mapping": {}}


def json_export(tmp_path, value=None, name="conversations.json"):
    path = tmp_path / name
    path.write_text(json.dumps(value if value is not None else [conversation()], ensure_ascii=False), encoding="utf-8")
    return path


def zip_export(tmp_path, members, compression=zipfile.ZIP_DEFLATED):
    path = tmp_path / "export.zip"
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, value in members:
            archive.writestr(name, value)
    return path


def test_original_bytes_are_immutable_and_reused(tmp_path):
    original = b'[ { "id" : "a", "mapping" : {}, "title":"\\u00e7" } ]\n'
    path = tmp_path / "conversations.json"
    path.write_bytes(original)
    first = prepare_source(path, tmp_path / "archive")
    second = prepare_source(path, tmp_path / "archive")
    assert first.sha256 == second.sha256 and first.path == second.path
    assert first.path.read_bytes() == original
    assert first.size == len(original)
    assert list(iter_conversations(first)) == [("conversations.json", 0, {"id": "a", "mapping": {}, "title": "ç"})]
    assert not list((tmp_path / "archive").glob(".pending-*"))


def test_existing_corrupt_object_is_never_overwritten(tmp_path):
    path = json_export(tmp_path)
    snapshot = prepare_source(path, tmp_path / "archive")
    snapshot.path.chmod(0o600)
    snapshot.path.write_bytes(b"damaged")
    with pytest.raises(SourceError, match="corrupt"):
        prepare_source(path, tmp_path / "archive")
    assert snapshot.path.read_bytes() == b"damaged"


def test_load_cas_verifies_bytes_without_copy_and_preserves_member_identity(tmp_path):
    path = json_export(tmp_path, name="downloaded-export.json")
    snapshot = prepare_source(path, tmp_path / "archive")
    loaded = load_source(snapshot.path, snapshot.sha256)
    assert loaded == snapshot
    assert list(iter_conversations(loaded)) == list(iter_conversations(snapshot))
    assert len(list((tmp_path / "archive").glob("objects/sha256/*/*"))) == 1
    with pytest.raises(SourceError, match="SHA256"):
        load_source(snapshot.path, "0" * 64)


def test_load_changed_source_is_rejected(tmp_path, monkeypatch):
    snapshot = prepare_source(json_export(tmp_path), tmp_path / "archive")
    real = source.os.fstat
    calls = 0

    def changed(fd):
        nonlocal calls
        result = real(fd)
        calls += 1
        return SimpleNamespace(st_mode=result.st_mode, st_dev=result.st_dev, st_ino=result.st_ino,
                               st_size=result.st_size, st_ctime_ns=result.st_ctime_ns,
                               st_mtime_ns=result.st_mtime_ns + (1 if calls > 1 else 0))

    monkeypatch.setattr(source.os, "fstat", changed)
    with pytest.raises(SourceError, match="changed"):
        load_source(snapshot.path, snapshot.sha256)


def test_zip_shards_and_complete_inventory(tmp_path):
    path = zip_export(tmp_path, [
        ("export/conversations-001.json", json.dumps([conversation("b")])),
        ("export/assets/photo.bin", b"asset-bytes"),
        ("export/conversations-000.json", json.dumps([conversation("a")])),
        ("export/assets/", b""),
    ])
    snapshot = prepare_source(path, tmp_path / "archive")
    assert snapshot.kind == "zip" and len(snapshot.inventory) == 4
    rows = list(iter_conversations(snapshot))
    assert [(member, ordinal, raw["id"]) for member, ordinal, raw in rows] == [
        ("export/conversations-000.json", 0, "a"), ("export/conversations-001.json", 0, "b")]
    assert all(item["crc_verified"] is False for item in snapshot.inventory)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/absolute", "folder\\file", "a//b", "a/./b"])
def test_unsafe_zip_names_are_rejected_without_extraction(tmp_path, name):
    path = zip_export(tmp_path, [("conversations.json", "[]"), (name, "payload")])
    with pytest.raises(SourceError, match="ZIP member"):
        prepare_source(path, tmp_path / "archive")
    assert not (tmp_path / "escape").exists()


def test_zip_link_is_rejected(tmp_path):
    info = zipfile.ZipInfo("link")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = zip_export(tmp_path, [("conversations.json", "[]"), (info, "../target")])
    with pytest.raises(SourceError, match="links"):
        prepare_source(path, tmp_path / "archive")


@pytest.mark.parametrize("members", [
    [("conversations.json", "[]"), ("conversations.json", "[]")],
    [("conversations-001.json", "[]")],
    [("conversations-000.json", "[]"), ("conversations-002.json", "[]")],
    [("conversations.json", "[]"), ("conversations-000.json", "[]")],
    [("a/conversations.json", "[]"), ("b/conversations.json", "[]")],
    [("chat.html", "no supported JSON")],
])
def test_ambiguous_missing_and_duplicate_formats_rejected(tmp_path, members):
    with pytest.warns(UserWarning) if members[0][0] == members[-1][0] and len(members) > 1 else __import__("contextlib").nullcontext():
        path = zip_export(tmp_path, members)
    with pytest.raises(SourceError):
        prepare_source(path, tmp_path / "archive")


def test_crc_corruption_fails_import(tmp_path):
    payload = json.dumps([conversation("unique-test-value")]).encode()
    path = zip_export(tmp_path, [("conversations.json", payload)], compression=zipfile.ZIP_STORED)
    original = path.read_bytes()
    offset = original.index(b"unique-test-value")
    path.write_bytes(original[:offset] + b"X" + original[offset + 1:])
    snapshot = prepare_source(path, tmp_path / "archive")
    with pytest.raises(SourceError, match="corrupt"):
        list(iter_conversations(snapshot))


@pytest.mark.parametrize("payload", [b"{\"conversations\": []}", b"not json", b"PKinvalid"])
def test_unrecognized_format_is_explicit(tmp_path, payload):
    path = tmp_path / "unknown"
    path.write_bytes(payload)
    with pytest.raises(SourceError):
        prepare_source(path, tmp_path / "archive")


@pytest.mark.parametrize("payload", [b"[{}", b"[1]", b"[{}] trailing", b"[{\"x\":NaN}]"])
def test_malformed_json_does_not_mark_complete(tmp_path, payload):
    path = tmp_path / "conversations.json"
    path.write_bytes(payload)
    snapshot = prepare_source(path, tmp_path / "archive")
    with pytest.raises(SourceError):
        list(iter_conversations(snapshot))


def test_duplicate_json_keys_are_not_silently_discarded(tmp_path):
    path = tmp_path / "conversations.json"
    path.write_bytes(b'[{"id":"a","mapping":{"node":{},"node":{"message":null}}}]')
    snapshot = prepare_source(path, tmp_path / "archive")
    with pytest.raises(SourceError, match="duplicate object key"):
        list(iter_conversations(snapshot))


def test_conversation_limit_is_applied_before_building_record(tmp_path):
    path = json_export(tmp_path, [{"id": "a", "text": "x" * 200_000}])
    limits = SourceLimits(max_conversation_bytes=10_000)
    snapshot = prepare_source(path, tmp_path / "archive", limits)
    with pytest.raises(SourceError, match="record exceeds"):
        list(iter_conversations(snapshot))


def test_archive_and_zip_expansion_limits(tmp_path):
    path = json_export(tmp_path)
    with pytest.raises(SourceError, match="source size"):
        prepare_source(path, tmp_path / "archive", SourceLimits(max_archive_bytes=8))
    zipped = zip_export(tmp_path, [("conversations.json", "[" + " " * 10_000 + "]")])
    with pytest.raises(SourceError, match="compression ratio"):
        prepare_source(zipped, tmp_path / "archive", SourceLimits(max_compression_ratio=2))


def test_changed_source_is_not_published(tmp_path, monkeypatch):
    path = json_export(tmp_path)
    real = source.os.fstat
    calls = 0

    def changed(fd):
        nonlocal calls
        result = real(fd)
        calls += 1
        return SimpleNamespace(st_mode=result.st_mode, st_size=result.st_size,
                               st_mtime_ns=result.st_mtime_ns + (1 if calls > 1 else 0))

    monkeypatch.setattr(source.os, "fstat", changed)
    with pytest.raises(SourceError, match="changed"):
        prepare_source(path, tmp_path / "archive")
    assert not list((tmp_path / "archive").glob("objects/sha256/*/*"))


def test_streaming_guard_handles_cross_chunk_escapes_and_branches():
    class TinyReader(io.BytesIO):
        def read(self, size=-1):
            return super().read(min(7, size) if size >= 0 else 7)

    values = [{"text": 'literal } { [ ] , \\" quotation " and newline\n', "id": str(i),
               "nested": [{"escape": "\\\\\\\""}]} for i in range(10)]
    raw = json.dumps(values, ensure_ascii=False).encode()
    assert [item for _, item in source._read_array(TinyReader(raw), SourceLimits(max_conversation_bytes=500))] == values


def test_utf8_bom_and_empty_array_supported(tmp_path):
    path = tmp_path / "conversations.json"
    path.write_bytes(b"\xef\xbb\xbf[]")
    snapshot = prepare_source(path, tmp_path / "archive")
    assert list(iter_conversations(snapshot)) == []


def test_source_symlink_rejected(tmp_path):
    original = json_export(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(original)
    with pytest.raises(SourceError, match="link"):
        prepare_source(link, tmp_path / "archive")
