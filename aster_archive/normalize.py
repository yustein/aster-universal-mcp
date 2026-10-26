"""Loss-aware normalization of the supported ChatGPT conversation mapping.

IDs and graph edges come from the export. Missing data stays missing. Original
bytes live in the source archive; canonical JSON is only a revision identity.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any


class NormalizationError(ValueError):
    """A conversation does not have a safely interpretable supported shape."""


def canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as error:
        raise NormalizationError("unsupported JSON value in conversation") from error


def content_hash(value: Any) -> str:
    try:
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    except UnicodeError as error:
        raise NormalizationError("invalid Unicode in conversation") from error


def _identifier(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or "\x00" in value:
        raise NormalizationError("missing or invalid stable source identifier")
    return value


def _timestamp(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _asset_refs(value: Any, path: str = "") -> list[dict]:
    """Collect declared opaque references without claiming asset availability."""
    references: list[dict] = []
    keys = {"asset_pointer", "asset_url", "file_id", "attachment_id", "image_url",
            "audio_url", "video_url", "download_url"}

    def visit(item: Any, prefix: str, context: str = "") -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                child_path = f"{prefix}.{key}" if prefix else key
                declared = key in keys or (key in {"url", "id"} and context in {"attachments", "image_url", "audio_url", "video_url"})
                if declared and isinstance(child, str) and child:
                    references.append({"ref": child, "path": child_path, "availability": "unverified"})
                visit(child, child_path, key if key in {"attachments", "image_url", "audio_url", "video_url"} else context)
        elif isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{prefix}.{index}", context)

    visit(value, path)
    return references


def _visible_text(message: dict | None) -> tuple[str, list[dict], bool]:
    if message is None:
        return "", [], False
    author = message.get("author")
    role = author.get("role") if isinstance(author, dict) else None
    metadata = message.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    channels = (message.get("channel"), metadata.get("channel"))
    content = message.get("content")
    # Unknown, reasoning, tool and internal execution content remains archived,
    # but is not exposed as ordinary searchable user/assistant prose.
    if role not in ("user", "assistant") or any(channel not in (None, "final", "commentary") for channel in channels):
        return "", [], False
    if message.get("recipient") not in (None, "all"):
        return "", [], False
    if not isinstance(content, dict) or content.get("content_type") not in ("text", "code", "multimodal_text"):
        return "", [], False
    source_parts: list[tuple[str, int, str]] = []
    if isinstance(content.get("parts"), list):
        source_parts = [(part, index, f"message.content.parts.{index}")
                        for index, part in enumerate(content["parts"]) if isinstance(part, str)]
    elif isinstance(content.get("text"), str):
        source_parts = [(content["text"], 0, "message.content.text")]
    pieces: list[str] = []
    parts: list[dict] = []
    position = 0
    for text, index, path in source_parts:
        if pieces:
            pieces.append("\n\n")
            position += 2
        start = position
        pieces.append(text)
        position += len(text)
        parts.append({"text": text, "start": start, "end": position,
                      "part_index": index, "path": path})
    joined = "".join(pieces)
    return joined, parts, any(bool(text) for text, _, _ in source_parts)


def normalize_conversation(raw: dict) -> dict:
    """Return a conversation revision and every declared graph node.

    This function does not flatten branches or infer project ownership. The
    caller supplies account/workspace/access scope independently of the export.
    Text offsets use Python Unicode code points and refer to the joined text;
    each text_parts entry identifies an exact original string. Separator bytes
    are deliberate presentation structure, never attributed to a source part.
    """
    if not isinstance(raw, dict):
        raise NormalizationError("conversation is not an object")
    identity = raw.get("conversation_id")
    if identity is None:
        identity = raw.get("id")
    conversation_id = _identifier(identity)
    mapping = raw.get("mapping")
    if not isinstance(mapping, dict):
        raise NormalizationError("unsupported conversation shape: mapping is required")
    nodes = []
    for node_id, node_value in sorted(mapping.items()):
        _identifier(node_id)
        if node_value is not None and not isinstance(node_value, dict):
            raise NormalizationError("conversation graph node is not an object")
        node = node_value or {}
        parent_id = _identifier(node.get("parent"), optional=True)
        children = node.get("children")
        if children is None:
            children = []
        if not isinstance(children, list):
            raise NormalizationError("conversation graph children must be an array")
        for child in children:
            _identifier(child)
        message = node.get("message")
        if message is not None and not isinstance(message, dict):
            raise NormalizationError("conversation message is not an object")
        author = message.get("author") if message else None
        role = author.get("role") if isinstance(author, dict) else None
        content = message.get("content") if message else None
        content_type = content.get("content_type") if isinstance(content, dict) else None
        text, parts, searchable = _visible_text(message)
        nodes.append({
            "node_id": node_id,
            "parent_id": parent_id,
            "children": list(children),
            "message_id": _identifier(message.get("id"), optional=True) if message else None,
            "role": role if isinstance(role, str) else None,
            "content_type": content_type if isinstance(content_type, str) else None,
            "created_at": _timestamp(message.get("create_time")) if message else None,
            "updated_at": _timestamp(message.get("update_time")) if message else None,
            "text": text,
            "text_parts": parts,
            "searchable": searchable,
            "asset_refs": _asset_refs(node_value),
            "content_hash": content_hash(node_value),
            "raw": node_value,
        })
    title = raw.get("title")
    return {
        "conversation_id": conversation_id,
        "title": title if isinstance(title, str) else None,
        "created_at": _timestamp(raw.get("create_time")),
        "updated_at": _timestamp(raw.get("update_time")),
        "current_node": _identifier(raw.get("current_node"), optional=True),
        "metadata": {key: value for key, value in raw.items() if key != "mapping"},
        "content_hash": content_hash(raw),
        "nodes": nodes,
    }
