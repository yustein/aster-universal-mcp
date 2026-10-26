import copy

import pytest

from aster_archive.normalize import NormalizationError, normalize_conversation


def message(identity, text, role="assistant", content_type="text", **extra):
    return {"id": identity, "author": {"role": role}, "create_time": 5.25,
            "content": {"content_type": content_type, "parts": [text]}, **extra}


def conversation():
    return {
        "id": "conversation-a", "title": "Aster / Türkçe", "create_time": 1.25,
        "update_time": 20, "current_node": "answer-b", "unknown_field": {"preserve": None},
        "mapping": {
            "root": {"id": "root", "message": None, "parent": None, "children": ["question"]},
            "question": {"message": message("question", "  İngilizce ve Türkçe?\n", "user"), "parent": "root", "children": ["answer-a", "answer-b"]},
            "answer-a": {"message": message("answer-a", "First answer"), "parent": "question", "children": []},
            "answer-b": {"message": message("answer-b", "Regenerated answer"), "parent": "question", "children": []},
        },
    }


def nodes(result):
    return {node["node_id"]: node for node in result["nodes"]}


def test_preserves_full_branch_graph_and_current_node():
    raw = conversation()
    result = normalize_conversation(raw)
    assert result["conversation_id"] == "conversation-a"
    assert result["current_node"] == "answer-b"
    assert set(nodes(result)) == {"root", "question", "answer-a", "answer-b"}
    assert nodes(result)["question"]["children"] == ["answer-a", "answer-b"]
    assert nodes(result)["root"]["message_id"] is None
    assert nodes(result)["answer-a"]["raw"] == raw["mapping"]["answer-a"]
    assert result["metadata"]["unknown_field"] == {"preserve": None}
    assert "mapping" not in result["metadata"]


def test_mapping_and_object_key_order_do_not_change_hashes():
    raw = conversation()
    reordered = dict(reversed(list(raw.items())))
    reordered["mapping"] = dict(reversed(list(raw["mapping"].items())))
    assert normalize_conversation(raw) == normalize_conversation(reordered)


def test_revision_changes_only_affected_node_hash():
    old = conversation()
    new = copy.deepcopy(old)
    new["mapping"]["answer-b"]["message"]["content"]["parts"] = ["Edited answer"]
    before, after = normalize_conversation(old), normalize_conversation(new)
    assert before["conversation_id"] == after["conversation_id"]
    assert before["content_hash"] != after["content_hash"]
    assert nodes(before)["answer-b"]["content_hash"] != nodes(after)["answer-b"]["content_hash"]
    assert nodes(before)["question"]["content_hash"] == nodes(after)["question"]["content_hash"]


def test_text_is_exact_and_parts_have_source_offsets_and_assets():
    raw = conversation()
    raw["mapping"]["question"]["message"]["content"] = {
        "content_type": "multimodal_text",
        "parts": ["  Çiğdem\n", {"content_type": "image_asset_pointer", "asset_pointer": "file-service://file-x"}, "Second\tpart  "],
    }
    raw["mapping"]["question"]["message"]["metadata"] = {"attachments": [{"id": "file-y", "name": "image.png"}]}
    result = nodes(normalize_conversation(raw))["question"]
    assert result["text"] == "  Çiğdem\n\n\nSecond\tpart  "
    assert [part["part_index"] for part in result["text_parts"]] == [0, 2]
    for part in result["text_parts"]:
        assert result["text"][part["start"]:part["end"]] == part["text"]
    assert {ref["ref"] for ref in result["asset_refs"]} == {"file-service://file-x", "file-y"}
    assert all(ref["availability"] == "unverified" for ref in result["asset_refs"])


@pytest.mark.parametrize("replacement", [
    message("a", "Internal text", content_type="thoughts"),
    message("a", "Internal text", content_type="reasoning_recap"),
    message("a", "Internal text", role="tool"),
    message("a", "Internal text", role="system"),
    message("a", "Internal text", channel="analysis"),
    message("a", "Internal text", metadata={"channel": "analysis"}, channel=None),
    message("a", "Internal text", recipient="python"),
    message("a", "Internal text", content_type="future_unknown"),
])
def test_internal_or_unknown_content_is_preserved_but_not_searchable(replacement):
    raw = conversation()
    raw["mapping"]["answer-a"]["message"] = replacement
    result = nodes(normalize_conversation(raw))["answer-a"]
    assert result["text"] == "" and result["searchable"] is False
    assert result["raw"]["message"] == replacement


def test_null_fields_and_dangling_edges_remain_distinguishable():
    raw = {"conversation_id": "a", "title": None, "create_time": None,
           "update_time": "unknown", "current_node": "missing", "mapping": {
               "a": {"parent": "not-in-export", "children": ["missing"], "message": None}, "null": None}}
    result = normalize_conversation(raw)
    assert result["title"] is None and result["created_at"] is None and result["updated_at"] is None
    assert result["metadata"]["update_time"] == "unknown"
    assert nodes(result)["a"]["parent_id"] == "not-in-export"
    assert nodes(result)["null"]["raw"] is None
    assert result["current_node"] == "missing"


@pytest.mark.parametrize("raw", [
    {"title": "titles are not IDs", "mapping": {}},
    {"id": "a"},
    {"id": "a", "mapping": []},
    {"id": "a", "mapping": {"node": {"children": "bad"}}},
    {"id": "a", "mapping": {"node": {"message": "bad"}}},
])
def test_unsupported_identity_and_shapes_are_explicit(raw):
    with pytest.raises(NormalizationError):
        normalize_conversation(raw)


def test_code_text_field_and_empty_parts():
    raw = conversation()
    raw["mapping"]["answer-a"]["message"]["content"] = {"content_type": "code", "text": "print('a')\n"}
    raw["mapping"]["answer-b"]["message"]["content"]["parts"] = ["", ""]
    result = nodes(normalize_conversation(raw))
    assert result["answer-a"]["text"] == "print('a')\n"
    assert result["answer-a"]["text_parts"][0]["path"] == "message.content.text"
    assert result["answer-b"]["searchable"] is False
