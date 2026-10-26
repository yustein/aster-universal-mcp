"""No corpus/model API calls: deterministic guards and optional local E5 smoke."""
import copy
import hashlib
import math
import os
from pathlib import Path

import pytest

from aster_archive import embeddings
from aster_archive.embeddings import (
    DIMENSIONS, EmbeddingError, LocalE5Encoder, MAX_TOKENS,
    generation_descriptor, generation_id, token_windows, validate_vector,
)


def test_generation_identity_changes_with_model_preprocessing_or_runtime():
    descriptor = generation_descriptor()
    assert generation_id(descriptor) == generation_id(dict(reversed(list(descriptor.items()))))
    for field, value in (("model_revision", "a" * 40), ("window_overlap_tokens", 32),
                         ("runtime", {"onnxruntime": "different"})):
        changed = copy.deepcopy(descriptor)
        changed[field] = value
        assert generation_id(changed) != generation_id(descriptor)


def test_token_windows_cover_every_token_with_prefix_and_bounded_overlap():
    prefix = [46692, 12]
    body = list(range(1000, 3401))
    windows = token_windows(body, prefix)
    assert all(len(window) <= MAX_TOKENS for window in windows)
    assert all(window[:3] == [0, *prefix] and window[-1] == 2 for window in windows)
    covered = set()
    for window in windows:
        covered.update(window[len(prefix) + 1:-1])
    assert covered == set(body)
    assert windows[-1][-2] == body[-1]
    assert token_windows([], prefix) == [[0, *prefix, 2]]


@pytest.mark.parametrize("vector", [[], [1.0] * DIMENSIONS,
    [math.nan] + [0.0] * (DIMENSIONS - 1), [math.inf] + [0.0] * (DIMENSIONS - 1)])
def test_invalid_embedding_vectors_are_never_publishable(vector):
    with pytest.raises(EmbeddingError, match="INVALID_VECTOR"):
        validate_vector(vector)


def test_corrupt_or_symlink_model_is_refused_without_network(tmp_path, monkeypatch):
    network = []
    monkeypatch.setattr(embeddings, "urlopen", lambda *args, **kwargs: network.append(args))
    (tmp_path / "model.onnx").write_bytes(b"corrupt synthetic artifact")
    with pytest.raises(EmbeddingError, match="CHECKSUM"):
        embeddings.prepare_model(tmp_path)
    assert network == []
    (tmp_path / "model.onnx").unlink()
    target = tmp_path / "outside-model"
    target.write_bytes(b"untouched")
    (tmp_path / "model.onnx").symlink_to(target)
    with pytest.raises(OSError):
        embeddings.prepare_model(tmp_path)
    assert network == [] and target.read_bytes() == b"untouched"


@pytest.mark.skipif(not os.environ.get("ASTER_ARCHIVE_TEST_MODEL_DIR"),
                    reason="requires explicitly prepared pinned local model; never downloads in tests")
def test_real_local_e5_multilingual_vectors_and_long_text_tail_are_covered():
    encoder = LocalE5Encoder(Path(os.environ["ASTER_ARCHIVE_TEST_MODEL_DIR"]), threads=2, batch_size=4)
    texts = [
        "We store private archive backups on our local computer.",
        "Özel arşiv yedeklerini yerel bilgisayarımızda saklıyoruz.",
        "The recipe uses tomatoes, olive oil and salt.",
        "başlangıç " * 700 + "Türkçe son kayıt korunur.",
    ]
    before = [hashlib.sha256(text.encode()).hexdigest() for text in texts]
    values = encoder.embed_passages(texts)
    query = encoder.embed_queries(["Where do we keep local backup copies?"])[0]
    scores = [sum(a * b for a, b in zip(query.vector, value.vector, strict=True)) for value in values]
    assert scores[0] > scores[2] and scores[1] > scores[2]
    assert values[-1].window_count > 1 and values[-1].token_count > MAX_TOKENS
    assert all(len(value.vector) == DIMENSIONS for value in values)
    assert all(abs(sum(x * x for x in value.vector) - 1) < 1e-5 for value in values)
    assert before == [hashlib.sha256(text.encode()).hexdigest() for text in texts]
    # One-window input exactly follows the publisher's prefix/special tokens;
    # standalone trailing-space tokenization must not change the real prefix.
    prefix = encoder._tokenizer.encode("passage:", add_special_tokens=False).ids
    full = encoder._tokenizer.encode("passage: " + texts[0], add_special_tokens=False).ids
    windows = token_windows(full[len(prefix):], prefix)
    expected = encoder._tokenizer.encode("passage: " + texts[0], add_special_tokens=True).ids
    assert windows == [expected]
