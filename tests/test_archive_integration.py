"""Data-integrity tests against an explicitly configured real PostgreSQL DB.

Set ASTER_ARCHIVE_TEST_DSN to a disposable integration database. These tests
never DROP/reset it: every test owns a fresh synthetic owner/source namespace.
No personal export content or paid model call is used.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import uuid

import psycopg
from psycopg.rows import dict_row
import pytest

from aster_archive.access import ArchiveScope
from aster_archive.source import prepare_source
from aster_archive.store import PostgresArchive


DSN = os.environ.get("ASTER_ARCHIVE_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires a disposable real PostgreSQL ASTER_ARCHIVE_TEST_DSN")


def chat(identity="chat-a", updated=20, answer="Archive anchor answer", *, title="Synthetic conversation"):
    def node(identity, role, text, parent, children):
        return {"id": identity, "parent": parent, "children": children, "message": {
            "id": identity, "author": {"role": role}, "create_time": 2.0,
            "content": {"content_type": "text", "parts": [text]}}}
    return {
        "conversation_id": identity, "title": title, "create_time": 1.0,
        "update_time": updated, "current_node": "answer", "future_metadata": {"unknown": None},
        "mapping": {
            "root": {"id": "root", "parent": None, "children": ["question"], "message": None},
            "question": node("question", "user", "Question common", "root", ["answer"]),
            "answer": node("answer", "assistant", answer, "question", []),
        },
    }


@pytest.fixture(scope="module")
def store():
    result = PostgresArchive(DSN)
    result.migrate()
    return result


@pytest.fixture
def scope():
    return ArchiveScope("integration-client", "integration-" + uuid.uuid4().hex, ("account-main",), None)


def sql(query, params=()):
    with psycopg.connect(DSN, row_factory=dict_row) as conn:
        return conn.execute(query, params).fetchall()


def counts(scope):
    owner, source = scope.owner_id, scope.source_ids[0]
    return sql("""SELECT
        (SELECT count(*) FROM aster_archive.conversations WHERE owner_id=%s AND source_id=%s) AS conversations,
        (SELECT count(*) FROM aster_archive.conversation_revisions WHERE owner_id=%s AND source_id=%s) AS revisions,
        (SELECT count(*) FROM aster_archive.passages p JOIN aster_archive.conversation_revisions r USING(revision_id)
          WHERE r.owner_id=%s AND r.source_id=%s) AS passages,
        (SELECT count(*) FROM aster_archive.derivations d JOIN aster_archive.conversation_revisions r USING(revision_id)
          WHERE r.owner_id=%s AND r.source_id=%s) AS derivations""", (owner, source) * 4)[0]


def register(store, tmp_path, scope, values, *, filename="export.json", indent=None):
    path = tmp_path / filename
    path.write_text(json.dumps(values, ensure_ascii=False, indent=indent), encoding="utf-8")
    source = prepare_source(path, tmp_path / "archive")
    return store.register_import(source, scope.owner_id, scope.source_ids[0])


def import_values(store, tmp_path, scope, values, **kwargs):
    from aster_archive.ingest import import_registered
    identity = register(store, tmp_path, scope, values, **kwargs)
    report = import_registered(store, identity, tmp_path / "archive")
    return identity, report


def current(scope, identity="chat-a"):
    return sql("""SELECT * FROM aster_archive.conversations
        WHERE owner_id=%s AND source_id=%s AND conversation_id=%s""",
               (scope.owner_id, scope.source_ids[0], identity))[0]


def first_hit(store, scope, query):
    result = store.search(scope, query)["results"]
    assert result, f"expected synthetic search match for {query!r}"
    return result[0]["id"]


def test_repeat_bytes_and_new_snapshot_occurrences_do_not_duplicate_content(store, scope, tmp_path):
    values = [chat()]
    first, report = import_values(store, tmp_path, scope, values)
    assert report["status"] == "complete"
    initial = counts(scope)
    assert initial == {"conversations": 1, "revisions": 1, "passages": 2, "derivations": 2}
    second, _ = import_values(store, tmp_path, scope, values)
    assert first == second and counts(scope) == initial
    third, _ = import_values(store, tmp_path, scope, values, filename="reexport.json", indent=2)
    assert third != first and counts(scope) == initial
    receipts = sql("SELECT import_id,status FROM aster_archive.import_items WHERE import_id=ANY(%s::text[])", ([first, third],))
    assert len(receipts) == 2 and all(row["status"] == "imported" for row in receipts)
    assert len(sql("SELECT * FROM aster_archive.import_outbox WHERE import_id=ANY(%s::text[])", ([first, third],))) == 2


def test_new_revision_branch_and_historical_exact_fetch(store, scope, tmp_path):
    old = chat(answer="VersionAlpha retained original")
    import_values(store, tmp_path, scope, [old])
    old_id = first_hit(store, scope, "VersionAlpha")
    original = store.fetch(scope, old_id)
    updated = copy.deepcopy(old)
    updated["update_time"] = 30
    updated["title"] = "Renamed synthetic conversation"
    updated["mapping"]["question"]["children"].append("regenerated")
    updated["mapping"]["regenerated"] = copy.deepcopy(updated["mapping"]["answer"])
    updated["mapping"]["regenerated"]["id"] = "regenerated"
    updated["mapping"]["regenerated"]["message"]["id"] = "regenerated"
    updated["mapping"]["regenerated"]["message"]["content"]["parts"] = ["VersionBeta regenerated response"]
    updated["current_node"] = "regenerated"
    import_values(store, tmp_path, scope, [updated], filename="new.json")
    assert counts(scope)["revisions"] == 2
    assert store.fetch(scope, old_id)["text"] == original["text"]
    assert store.fetch(scope, old_id)["metadata"]["is_current_revision"] is False
    latest = store.fetch(scope, first_hit(store, scope, "VersionBeta"))
    assert latest["title"] == "Renamed synthetic conversation"
    assert latest["metadata"]["current_node"] == "regenerated"
    rows = sql("SELECT node_id FROM aster_archive.nodes WHERE revision_id=%s", (current(scope)["current_revision"],))
    assert {row["node_id"] for row in rows} == {"root", "question", "answer", "regenerated"}


def test_backfilled_older_export_does_not_roll_back_latest(store, scope, tmp_path):
    import_values(store, tmp_path, scope, [chat(updated=30, answer="LatestAnchor")])
    before = current(scope)
    import_values(store, tmp_path, scope, [chat(updated=10, answer="OldAnchor")], filename="older.json")
    assert current(scope)["current_revision"] == before["current_revision"]
    assert current(scope)["current_updated"] == 30
    assert store.search(scope, "OldAnchor")["results"] == []
    assert first_hit(store, scope, "LatestAnchor")
    assert counts(scope)["revisions"] == 2


@pytest.mark.parametrize("timestamp", [None, 20])
def test_unordered_conflicting_revisions_surface_ambiguity(store, scope, tmp_path, timestamp):
    import_values(store, tmp_path, scope, [chat(updated=timestamp, answer="EarlierObservation")])
    pointer = current(scope)["current_revision"]
    import_values(store, tmp_path, scope, [chat(updated=timestamp, answer="LaterObservation")], filename="ambiguous.json")
    state = current(scope)
    assert state["current_revision"] == pointer and state["current_ambiguous"] is True
    assert store.coverage(scope)["ambiguous_current_revisions"] == 1
    evidence = store.fetch(scope, first_hit(store, scope, "EarlierObservation"))
    assert evidence["metadata"]["current_revision_ambiguous"] is True


@pytest.mark.parametrize("conflicting_timestamp,remains_ambiguous", [(None, True), (10, False)])
def test_advancing_known_timestamp_preserves_only_unresolved_chronology(store, scope, tmp_path,
                                                                      conflicting_timestamp, remains_ambiguous):
    import_values(store, tmp_path, scope, [chat(updated=10, answer="First dated observation")])
    import_values(store, tmp_path, scope, [chat(updated=conflicting_timestamp, answer="Conflicting observation")],
                  filename="conflicting.json")
    assert current(scope)["current_ambiguous"] is True
    import_values(store, tmp_path, scope, [chat(updated=20, answer="Newest dated observation")], filename="newest.json")
    state = current(scope)
    assert state["current_updated"] == 20
    assert state["current_ambiguous"] is remains_ambiguous
    fetched = store.fetch(scope, first_hit(store, scope, '"Newest dated"'))
    assert fetched["metadata"]["current_revision_ambiguous"] is remains_ambiguous


def test_interruption_after_one_checkpoint_resumes_without_duplication(store, scope, tmp_path, monkeypatch):
    from aster_archive import ingest
    identity = register(store, tmp_path, scope, [chat("a"), chat("b"), chat("c")])
    real_iterator = ingest.iter_conversations

    def interrupted(source):
        iterator = real_iterator(source)
        yield next(iterator)
        raise RuntimeError("synthetic interruption between records")

    monkeypatch.setattr(ingest, "iter_conversations", interrupted)
    try:
        ingest.import_registered(store, identity, tmp_path / "archive")
    except RuntimeError:
        pass
    assert store.import_report(identity)["status"] == "failed"
    assert counts(scope) == {"conversations": 1, "revisions": 1, "passages": 2, "derivations": 2}
    assert len(sql("SELECT * FROM aster_archive.import_items WHERE import_id=%s", (identity,))) == 1
    monkeypatch.setattr(ingest, "iter_conversations", real_iterator)
    assert ingest.import_registered(store, identity, tmp_path / "archive")["status"] == "complete"
    assert counts(scope) == {"conversations": 3, "revisions": 3, "passages": 6, "derivations": 6}


def test_failure_before_receipt_rolls_back_revision_passages_and_derivations(store, scope, tmp_path, monkeypatch):
    from aster_archive import ingest
    identity = register(store, tmp_path, scope, [chat()])
    real_lock = store.import_lock

    class FaultConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, statement, *args, **kwargs):
            if isinstance(statement, str) and "INSERT INTO aster_archive.import_items" in statement:
                raise RuntimeError("synthetic failure before checkpoint receipt")
            return self.connection.execute(statement, *args, **kwargs)

    @contextmanager
    def failing_lock(owner, source):
        with real_lock(owner, source) as connection:
            yield FaultConnection(connection)

    monkeypatch.setattr(store, "import_lock", failing_lock)
    try:
        ingest.import_registered(store, identity, tmp_path / "archive")
    except RuntimeError:
        pass
    assert counts(scope) == {"conversations": 0, "revisions": 0, "passages": 0, "derivations": 0}
    assert sql("SELECT * FROM aster_archive.import_items WHERE import_id=%s", (identity,)) == []
    assert store.import_report(identity)["status"] == "failed"
    assert len(sql("SELECT * FROM aster_archive.import_outbox WHERE import_id=%s", (identity,))) == 1
    monkeypatch.setattr(store, "import_lock", real_lock)
    assert ingest.import_registered(store, identity, tmp_path / "archive")["status"] == "complete"
    assert counts(scope)["derivations"] == 2
    held = sql("""SELECT kind,state FROM aster_archive.derivations d
        JOIN aster_archive.conversation_revisions r USING(revision_id) WHERE owner_id=%s""", (scope.owner_id,))
    assert {(row["kind"], row["state"]) for row in held} == {("embedding", "held"), ("knowledge", "held")}


def test_owner_source_and_project_scope_apply_to_all_reads(store, scope, tmp_path):
    other_source = ArchiveScope(scope.client_id, scope.owner_id, ("account-other",), None)
    other_owner = ArchiveScope(scope.client_id, "other-" + uuid.uuid4().hex, ("account-main",), None)
    import_values(store, tmp_path, scope, [chat(answer="ScopeAnchor allowed")], filename="allowed.json")
    import_values(store, tmp_path, other_source, [chat(answer="ScopeAnchor secret-source")], filename="source.json")
    import_values(store, tmp_path, other_owner, [chat(answer="ScopeAnchor secret-owner")], filename="owner.json")
    assert len(store.search(scope, "ScopeAnchor")["results"]) == 1
    assert store.coverage(scope)["conversations"] == 1
    for forbidden_scope in (other_source, other_owner):
        forbidden = first_hit(store, forbidden_scope, "ScopeAnchor")
        with pytest.raises(LookupError):
            store.fetch(scope, forbidden)
        with pytest.raises(LookupError):
            store.context(scope, forbidden)
    allowed = first_hit(store, scope, "ScopeAnchor")
    project = ArchiveScope(scope.client_id, scope.owner_id, scope.source_ids, ("project-one",))
    assert store.search(project, "ScopeAnchor")["results"] == []
    assert store.coverage(project)["conversations"] == 0
    with pytest.raises(LookupError):
        store.fetch(project, allowed)
    sql("UPDATE aster_archive.conversations SET project_id=%s WHERE owner_id=%s AND source_id=%s RETURNING conversation_id",
        ("project-one", scope.owner_id, scope.source_ids[0]))
    assert len(store.search(project, "ScopeAnchor")["results"]) == 1
    assert store.coverage(project)["conversations"] == 1
    assert store.coverage(project)["imports"] is None
    denied = ArchiveScope(scope.client_id, scope.owner_id, (), None)
    assert store.search(denied, "ScopeAnchor")["results"] == []
    assert store.coverage(denied)["conversations"] == 0


def test_branch_context_pagination_excludes_sibling_and_binds_cursor(store, scope, tmp_path):
    value = chat(answer="SiblingA excluded")
    branch = copy.deepcopy(value["mapping"]["answer"])
    branch["id"] = branch["message"]["id"] = "sibling-b"
    branch["message"]["content"]["parts"] = ["SiblingB selected"]
    value["mapping"]["sibling-b"] = branch
    value["mapping"]["question"]["children"].append("sibling-b")
    value["current_node"] = "sibling-b"
    import_values(store, tmp_path, scope, [value])
    target = first_hit(store, scope, "SiblingB")
    page = store.context(scope, target, limit=1)
    assert page["next_cursor"] is not None
    with pytest.raises(ValueError):
        store.context(scope, first_hit(store, scope, "SiblingA"), limit=1, cursor=page["next_cursor"])
    passages = page["passages"][:]
    while page["next_cursor"]:
        page = store.context(scope, target, limit=1, cursor=page["next_cursor"])
        passages.extend(page["passages"])
    assert [row["text"] for row in passages] == ["Question common", "SiblingB selected"]
    assert len({row["id"] for row in passages}) == 2
    assert page["ancestry_status"] == "complete"


def test_quoted_phrase_is_exact_and_unicode_is_preserved(store, scope, tmp_path):
    import_values(store, tmp_path, scope, [
        chat("exact", answer="Çiğdem: Coordinator fleet çalışma hazır."),
        chat("near", answer="Coordinator and fleet is ready."),
    ])
    hits = store.search(scope, '"Coordinator fleet"')["results"]
    assert len(hits) == 1
    assert store.fetch(scope, hits[0]["id"])["text"] == "Çiğdem: Coordinator fleet çalışma hazır."
    assert len(store.search(scope, '"Çiğdem"')["results"]) == 1
    assert len(store.search(scope, "çalışma")["results"]) == 1


def test_long_messages_are_exact_bounded_passages_with_valid_hashes(store, scope, tmp_path):
    original = ("Uzun metin Çiğdem — Aster Coordinator.\n" * 800) + "last byte"
    value = chat(answer=original)
    value["mapping"]["answer"]["message"]["content"]["parts"].append("  Separate part\t")
    import_values(store, tmp_path, scope, [value])
    passages = sql("""SELECT p.* FROM aster_archive.passages p
        JOIN aster_archive.conversation_revisions r USING(revision_id)
        WHERE owner_id=%s AND node_id='answer' ORDER BY part_index,start_char""", (scope.owner_id,))
    first_part = [row for row in passages if row["part_index"] == 0]
    assert len(first_part) > 1
    rebuilt = ""
    for row in first_part:
        assert row["start_char"] <= len(rebuilt)
        assert row["text"] == original[row["start_char"]:row["end_char"]]
        rebuilt += row["text"][len(rebuilt) - row["start_char"]:]
    assert rebuilt == original
    assert [row["text"] for row in passages if row["part_index"] == 1] == ["  Separate part\t"]
    for row in passages:
        assert row["text_sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()
        assert len(row["text"]) == row["end_char"] - row["start_char"]
        fetched = store.fetch(scope, row["passage_id"])
        assert fetched["text"] == row["text"]
        assert fetched["metadata"]["passage_window_chars"] == 2000
        assert fetched["metadata"]["passage_stride_chars"] == 1000
        assert fetched["metadata"]["overlap_with_previous_chars"] == (min(1000, len(row["text"])) if row["start_char"] else 0)


def test_search_finds_quoted_phrase_and_words_crossing_passage_boundary(store, scope, tmp_path):
    phrase = "Coordinator fleet boundary"
    english = "Coordination"
    turkish = "Çalışmalarımız"
    values = [
        chat("phrase", answer=("x " * 997) + phrase + " trailing."),
        chat("english", answer=("y " * 998) + english + " process."),
        chat("turkish", answer=("z " * 998) + turkish + " sürüyor."),
    ]
    import_values(store, tmp_path, scope, values)
    for query, expected in [(f'"{phrase}"', phrase), (english, english), (turkish, turkish)]:
        matches = store.search(scope, query)["results"]
        assert matches
        assert any(expected in store.fetch(scope, row["id"])["text"] for row in matches)


def test_passage_windows_stop_at_end_without_fully_redundant_tail(store, scope, tmp_path):
    import_values(store, tmp_path, scope, [chat(answer="a " * 1000 + "!")])
    windows = sql("""SELECT start_char,end_char FROM aster_archive.passages p
        JOIN aster_archive.conversation_revisions r USING(revision_id)
        WHERE owner_id=%s AND node_id='answer' ORDER BY start_char""", (scope.owner_id,))
    assert windows == [{"start_char": 0, "end_char": 2000}, {"start_char": 1000, "end_char": 2001}]


def test_tombstone_hides_history_and_prevents_old_export_resurrection(store, scope, tmp_path):
    value = chat(answer="DeletedAnchor")
    import_values(store, tmp_path, scope, [value])
    target = first_hit(store, scope, "DeletedAnchor")
    store.tombstone(scope.owner_id, scope.source_ids[0], "chat-a")
    assert store.search(scope, "DeletedAnchor")["results"] == []
    with pytest.raises(LookupError):
        store.fetch(scope, target)
    with pytest.raises(LookupError):
        store.context(scope, target)
    identity, report = import_values(store, tmp_path, scope, [value], filename="older-again.json", indent=2)
    assert report["status"] == "complete"
    assert sql("SELECT status FROM aster_archive.import_items WHERE import_id=%s", (identity,)) == [{"status": "tombstoned"}]
    assert current(scope)["tombstoned_at"] is not None
    assert store.coverage(scope)["conversations"] == 0


def test_invalid_record_is_receipted_without_discarding_valid_records(store, scope, tmp_path):
    identity, report = import_values(store, tmp_path, scope, [chat(), {"title": "No stable identity", "mapping": {}}])
    assert report["status"] == "complete_with_errors"
    items = sql("SELECT ordinal,status,error_code FROM aster_archive.import_items WHERE import_id=%s ORDER BY ordinal", (identity,))
    assert [item["status"] for item in items] == ["imported", "invalid"]
    assert items[1]["error_code"]
    assert counts(scope)["conversations"] == 1


def test_json_nul_is_preserved_in_original_and_receipted_as_database_gap(store, scope, tmp_path):
    value = chat(answer="Text remains in the original snapshot")
    value["future_metadata"] = {"opaque": "before\x00after"}
    identity, report = import_values(store, tmp_path, scope, [value])
    assert report["status"] == "complete_with_errors"
    assert sql("SELECT status,error_code FROM aster_archive.import_items WHERE import_id=%s", (identity,)) == [
        {"status": "invalid", "error_code": "unrepresentable_database_value"}]
    assert counts(scope) == {"conversations": 0, "revisions": 0, "passages": 0, "derivations": 0}
    receipt = store.get_import(identity)
    original = json.loads(Path(receipt["object_path"]).read_text(encoding="utf-8"))
    assert original[0]["future_metadata"]["opaque"] == "before\x00after"


def test_parse_failure_is_not_false_complete_on_retry(store, scope, tmp_path):
    from aster_archive.ingest import import_registered
    path = tmp_path / "broken.json"
    path.write_text("[" + json.dumps(chat()) + ", {", encoding="utf-8")
    snapshot = prepare_source(path, tmp_path / "archive")
    identity = store.register_import(snapshot, scope.owner_id, scope.source_ids[0])
    for _ in range(2):
        try:
            import_registered(store, identity, tmp_path / "archive")
        except ValueError:
            pass
        report = store.import_report(identity)
        assert report["status"] == "failed" and report["error_code"]
        assert counts(scope)["revisions"] <= 1


def test_missing_cas_object_records_failure_and_recovers_after_restore(store, scope, tmp_path):
    from aster_archive.ingest import import_registered
    identity = register(store, tmp_path, scope, [chat()])
    receipt = store.get_import(identity)
    object_path = Path(receipt["object_path"])
    object_path.unlink()
    with pytest.raises(FileNotFoundError):
        import_registered(store, identity, tmp_path / "archive")
    failed = store.import_report(identity)
    assert failed["status"] == "failed" and failed["error_code"] == "FileNotFoundError"
    assert counts(scope)["revisions"] == 0
    restored = prepare_source(tmp_path / "export.json", tmp_path / "archive")
    assert restored.sha256 == receipt["archive_sha256"] and restored.path == object_path
    assert import_registered(store, identity, tmp_path / "archive")["status"] == "complete"
    assert counts(scope)["revisions"] == 1


def test_coverage_is_json_serializable_through_mcp_reader(store, scope, tmp_path, monkeypatch):
    from aster_archive import tools
    import_values(store, tmp_path, scope, [chat()])
    monkeypatch.setattr(tools, "current_scope", lambda path: scope)
    reader = tools.ArchiveReader(store, origin="https://archive.example.test", grants_path=tmp_path / "unused-grants")
    result = reader.coverage()
    assert result["conversations"] == 1
    assert result["available_account_total"] is None
    assert result["complete_account_history_verified"] is False
    assert json.loads(json.dumps(result))["conversations"] == 1
