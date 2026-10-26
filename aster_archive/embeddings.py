"""Pinned local CPU embeddings; only explicit model preparation uses a network.

Canonical passages are never changed. Long model inputs are covered by token
windows, mean pooled individually, then averaged and normalized into one vector.
This derived representation is a retrieval aid, not a replacement for evidence.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import stat
import tempfile
import threading
from typing import Sequence
from urllib.request import Request, urlopen

from .storage import require_storage_reserve


MODEL_ID = "intfloat/multilingual-e5-small"
MODEL_REVISION = "fd1525a9fd15316a2d503bf26ab031a61d056e98"
MODEL_FILES = {
    "model.onnx": ("onnx/model.onnx", "ca456c06b3a9505ddfd9131408916dd79290368331e7d76bb621f1cba6bc8665", 512 * 1024**2),
    "tokenizer.json": ("tokenizer.json", "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39", 32 * 1024**2),
}
RUNTIME_VERSIONS = {"onnxruntime": "1.23.2", "tokenizers": "0.22.1", "numpy": "2.3.4"}
DIMENSIONS = 384
MAX_TOKENS = 512
WINDOW_OVERLAP = 64
MAX_INPUT_CHARS = 8192
MAX_WINDOWS = 64


class EmbeddingError(RuntimeError):
    """Sanitized local model/configuration failure."""


@dataclass(frozen=True)
class EmbeddedText:
    vector: Sequence[float]
    token_count: int
    window_count: int


def generation_descriptor() -> dict:
    return {
        "model_id": MODEL_ID, "model_revision": MODEL_REVISION,
        "files": {name: value[1] for name, value in MODEL_FILES.items()},
        "dimensions": DIMENSIONS, "runtime": dict(RUNTIME_VERSIONS),
        "provider": "CPUExecutionProvider", "pipeline": "aster-e5-onnx-v1",
        "query_prefix": "query: ", "passage_prefix": "passage: ",
        "max_tokens": MAX_TOKENS, "window_overlap_tokens": WINDOW_OVERLAP,
        "pooling": "attention-masked-mean-including-specials-and-prefix",
        "window_reduction": "mean-of-l2-normalized-windows-then-l2",
        "long_text": "all-body-tokens-covered; no silent truncation",
        "source_alignment": "canonical Unicode text and passage offsets remain unchanged",
    }


def generation_id(descriptor: dict) -> str:
    if not isinstance(descriptor, dict) or descriptor.get("dimensions") != DIMENSIONS:
        raise ValueError("invalid embedding generation")
    value = json.dumps(descriptor, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":"), allow_nan=False)
    if len(value) > 16384:
        raise ValueError("oversized embedding generation")
    return hashlib.sha256(value.encode()).hexdigest()


def validate_vector(value: Sequence[float]) -> list[float]:
    try:
        if len(value) != DIMENSIONS:
            raise ValueError()
        result = [float(item) for item in value]
        if not all(math.isfinite(item) for item in result):
            raise ValueError()
        norm = math.sqrt(math.fsum(item * item for item in result))
        if not 0.999 <= norm <= 1.001:
            raise ValueError()
        return result
    except (TypeError, ValueError, OverflowError):
        raise EmbeddingError("ARCHIVE_EMBEDDING_INVALID_VECTOR") from None


def _verified_file(path: Path, expected: str, maximum: int) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 1 <= before.st_size <= maximum:
            raise EmbeddingError("ARCHIVE_MODEL_ARTIFACT_INVALID")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
    identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
    if digest != expected or identity(before) != identity(after):
        raise EmbeddingError("ARCHIVE_MODEL_CHECKSUM_MISMATCH")


def prepare_model(model_dir: Path) -> dict:
    """Download only the two public allowlisted artifacts, with pinned SHA-256.

    This local operator action never receives or transmits conversation text.
    Existing corrupt files are refused rather than silently replaced.
    """
    model_dir = Path(model_dir)
    if not model_dir.is_absolute() or model_dir.is_symlink():
        raise ValueError("absolute regular model directory required")
    model_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, (remote, expected, maximum) in MODEL_FILES.items():
        destination = model_dir / name
        if destination.exists() or destination.is_symlink():
            _verified_file(destination, expected, maximum)
            continue
        require_storage_reserve(model_dir, additional_bytes=maximum)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".model-", dir=model_dir, delete=False) as output:
                temporary = Path(output.name)
                digest, size = hashlib.sha256(), 0
                request = Request(
                    f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{remote}",
                    headers={"User-Agent": "aster-archive-public-model-preparation/1"},
                )
                with urlopen(request, timeout=60) as response:
                    if response.status != 200:
                        raise EmbeddingError("ARCHIVE_MODEL_DOWNLOAD_FAILED")
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > maximum:
                            raise EmbeddingError("ARCHIVE_MODEL_ARTIFACT_TOO_LARGE")
                        digest.update(chunk)
                        output.write(chunk)
                if not size or digest.hexdigest() != expected:
                    raise EmbeddingError("ARCHIVE_MODEL_CHECKSUM_MISMATCH")
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o400)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                _verified_file(destination, expected, maximum)
            directory_fd = os.open(model_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    descriptor = generation_descriptor()
    return {"status": "model_ready", "model_id": MODEL_ID,
            "model_revision": MODEL_REVISION, "generation_id": generation_id(descriptor)}


def token_windows(body: Sequence[int], prefix: Sequence[int]) -> list[list[int]]:
    """Cover every body token; special token IDs are verified against the pin."""
    available = MAX_TOKENS - len(prefix) - 2
    if available <= WINDOW_OVERLAP:
        raise EmbeddingError("ARCHIVE_MODEL_PREFIX_INVALID")
    if not body:
        return [[0, *prefix, 2]]
    result = []
    for offset in range(0, len(body), available - WINDOW_OVERLAP):
        window = body[offset:offset + available]
        result.append([0, *prefix, *window, 2])
        if len(result) > MAX_WINDOWS:
            raise EmbeddingError("ARCHIVE_EMBEDDING_INPUT_LIMIT")
        if offset + len(window) == len(body):
            break
    return result


class LocalE5Encoder:
    """Verified on-disk model; no hub client, remote code, or implicit download."""

    def __init__(self, model_dir: Path, *, threads: int = 2, batch_size: int = 4):
        if type(threads) is not int or not 1 <= threads <= 4:
            raise ValueError("embedding threads must be 1..4")
        if type(batch_size) is not int or not 1 <= batch_size <= 16:
            raise ValueError("embedding batch size must be 1..16")
        self.model_dir = Path(model_dir)
        if not self.model_dir.is_absolute() or self.model_dir.is_symlink():
            raise ValueError("absolute regular model directory required")
        for name, (_, expected, maximum) in MODEL_FILES.items():
            _verified_file(self.model_dir / name, expected, maximum)
        if any(version(name) != expected for name, expected in RUNTIME_VERSIONS.items()):
            raise EmbeddingError("ARCHIVE_EMBEDDING_RUNTIME_VERSION_MISMATCH")
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self._tokenizer.no_padding()
        self._tokenizer.no_truncation()
        if [self._tokenizer.token_to_id(item) for item in ("<s>", "<pad>", "</s>")] != [0, 1, 2]:
            raise EmbeddingError("ARCHIVE_MODEL_TOKENIZER_CONTRACT_MISMATCH")
        options = ort.SessionOptions()
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.add_session_config_entry("session.intra_op.allow_spinning", "0")
        options.add_session_config_entry("session.inter_op.allow_spinning", "0")
        options.log_severity_level = 3
        self._session = ort.InferenceSession(
            str(self.model_dir / "model.onnx"), sess_options=options,
            providers=["CPUExecutionProvider"],
        )
        self._inputs = {value.name for value in self._session.get_inputs()}
        if not {"input_ids", "attention_mask"} <= self._inputs <= {
            "input_ids", "attention_mask", "token_type_ids",
        }:
            raise EmbeddingError("ARCHIVE_MODEL_INPUT_CONTRACT_MISMATCH")
        self.descriptor = generation_descriptor()
        self.generation_id = generation_id(self.descriptor)

    def _infer(self, windows: list[list[int]]):
        np = self._np
        vectors = []
        for offset in range(0, len(windows), self.batch_size):
            batch = windows[offset:offset + self.batch_size]
            length = max(map(len, batch))
            ids = np.ones((len(batch), length), dtype=np.int64)
            mask = np.zeros_like(ids)
            for index, tokens in enumerate(batch):
                ids[index, :len(tokens)] = tokens
                mask[index, :len(tokens)] = 1
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self._session.run(None, feed)[0]
            if hidden.shape != (len(batch), length, DIMENSIONS):
                raise EmbeddingError("ARCHIVE_MODEL_OUTPUT_CONTRACT_MISMATCH")
            pooled = (hidden * mask[..., None]).sum(axis=1) / mask.sum(axis=1)[:, None]
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            if not np.isfinite(pooled).all() or not (norms > 0).all():
                raise EmbeddingError("ARCHIVE_EMBEDDING_INVALID_VECTOR")
            vectors.extend(pooled / norms)
        return vectors

    def _encode(self, texts: Sequence[str], prefix: str) -> list[EmbeddedText]:
        if (not isinstance(texts, (tuple, list)) or not 1 <= len(texts) <= 64
                or any(not isinstance(text, str) or len(text) > MAX_INPUT_CHARS or "\x00" in text
                       for text in texts)):
            raise ValueError("invalid bounded embedding input")
        with self._lock:
            # A standalone trailing space is a separate SentencePiece token;
            # with actual text it merges into the first body token. Split after
            # the colon while encoding the complete prefixed input unchanged.
            prefix_ids = self._tokenizer.encode(prefix.rstrip(), add_special_tokens=False).ids
            windows, ranges = [], []
            for text in texts:
                encoded = self._tokenizer.encode(prefix + text, add_special_tokens=False).ids
                if encoded[:len(prefix_ids)] != prefix_ids:
                    raise EmbeddingError("ARCHIVE_MODEL_PREFIX_INVALID")
                body = encoded[len(prefix_ids):]
                pieces = token_windows(body, prefix_ids)
                ranges.append((len(windows), len(pieces), len(body)))
                windows.extend(pieces)
            vectors = self._infer(windows)
            result = []
            for offset, count, token_count in ranges:
                vector = self._np.mean(vectors[offset:offset + count], axis=0)
                vector /= self._np.linalg.norm(vector)
                result.append(EmbeddedText(validate_vector(vector), token_count, count))
            return result

    def embed_passages(self, texts: Sequence[str]) -> list[EmbeddedText]:
        return self._encode(texts, "passage: ")

    def embed_queries(self, texts: Sequence[str]) -> list[EmbeddedText]:
        return self._encode(texts, "query: ")
