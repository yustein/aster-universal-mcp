"""Bounded enumeration, honest totals, stable anchors and confined path checks."""
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import filesystem


def test_small_directory_preserves_metadata_hidden_entries_and_exact_count(tmp_path):
    (tmp_path / ".hidden").write_text("abc")
    (tmp_path / "folder").mkdir()
    result = filesystem.list_directory(".", root=str(tmp_path), limit=3)
    assert result["total"] == 2 and result["total_is_exact"] and not result["truncated"]
    assert [entry["name"] for entry in result["entries"]] == [".hidden", "folder"]
    hidden = result["entries"][0]
    assert hidden["size"] == 3 and not hidden["is_directory"] and not hidden["is_symlink"]
    assert hidden["modified_utc"].endswith("+00:00")
    assert set(hidden) == {"name", "mode", "size", "modified_utc", "is_directory", "is_symlink", "uid", "gid"}


@pytest.mark.parametrize("names_only", [False, True])
def test_large_directory_scans_only_limit_plus_one_and_reports_unknown_total(tmp_path, monkeypatch, names_only):
    for number in range(8):
        (tmp_path / f"entry-{number}").write_text("x")
    real_scandir = os.scandir
    observed = {"next_calls": 0, "closed": False}
    class BoundedScan:
        def __init__(self, target):
            self.inner = real_scandir(target)
        def __enter__(self):
            return self
        def __iter__(self):
            return self
        def __next__(self):
            observed["next_calls"] += 1
            assert observed["next_calls"] <= 4, "enumeration read beyond limit + 1"
            return next(self.inner)
        def __exit__(self, *args):
            observed["closed"] = True
            self.inner.close()
    monkeypatch.setattr(filesystem.os, "scandir", BoundedScan)
    result = filesystem.list_directory(str(tmp_path), limit=3, names_only=names_only)
    assert observed == {"next_calls": 4, "closed": True}
    assert len(result["entries"]) == 3
    assert result["truncated"] and result["total"] is None and not result["total_is_exact"]
    assert "Total is unknown" in result["note"]
    assert all(isinstance(entry, str if names_only else dict) for entry in result["entries"])


def test_exact_limit_is_not_truncated_and_empty_directory_has_zero_total(tmp_path):
    assert filesystem.list_directory(str(tmp_path), limit=1)["total"] == 0
    (tmp_path / "one").touch()
    result = filesystem.list_directory(str(tmp_path), limit=1)
    assert result["total"] == 1 and result["total_is_exact"] and not result["truncated"]


def test_explicit_listing_anchor_does_not_follow_process_chdir(tmp_path, monkeypatch):
    anchor, elsewhere = tmp_path / "anchor", tmp_path / "elsewhere"
    anchor.mkdir()
    elsewhere.mkdir()
    (anchor / "anchored").touch()
    monkeypatch.chdir(elsewhere)
    for options in ({"root": str(anchor)}, {"base": str(anchor), "names_only": True}):
        result = filesystem.list_directory(".", **options)
        assert result["path"] == str(anchor)
        assert len(result["entries"]) == 1


def test_root_boundary_rejects_absolute_and_relative_escape(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    for path in (str(outside), "../outside"):
        with pytest.raises(filesystem.DirectoryAccessDenied):
            filesystem.list_directory(path, root=str(root))
    # Removing the explicit boundary supports an already-authorized absolute path.
    assert filesystem.list_directory(str(outside), base=str(root))["path"] == str(outside)


def test_root_boundary_resolves_and_rejects_symlink_escape(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("host does not permit creating this symlink fixture")
    for path in ("escape", str(link)):
        with pytest.raises(filesystem.DirectoryAccessDenied):
            filesystem.list_directory(path, root=str(root))


@pytest.mark.parametrize("limit", [0, 201, True, 1.5, "2"])
def test_invalid_limit_is_rejected_before_directory_scan(tmp_path, monkeypatch, limit):
    def must_not_scan(path):
        raise AssertionError("invalid limit reached filesystem enumeration")
    monkeypatch.setattr(filesystem.os, "scandir", must_not_scan)
    with pytest.raises(ValueError, match="limit"):
        filesystem.list_directory(str(tmp_path), limit=limit)
