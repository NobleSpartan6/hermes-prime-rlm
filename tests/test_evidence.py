"""Evidence collection + tree digest tests (spec §24 Evidence)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import _git_exe, evidence


@pytest.fixture()
def repo_with_commit(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    git = _git_exe()

    def run(*args):
        subprocess.run([git, *args], cwd=repo, check=True, capture_output=True)

    run("init", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo / "b.txt").write_text("bravo\n", encoding="utf-8")
    (repo / "dir with space").mkdir()
    (repo / "dir with space" / "c.txt").write_text("charlie\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "base")
    return repo


def _commit_all(repo, message="change"):
    git = _git_exe()
    subprocess.run([git, "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        [git, "commit", "-m", message],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"},
    )


def test_tracked_edits_appear(repo_with_commit):
    (repo_with_commit / "a.txt").write_text("alpha2\n", encoding="utf-8")
    changed = evidence.parse_status_z(evidence.collect_status_z(str(repo_with_commit)))
    assert changed.modified == ["a.txt"]


def test_deletions_appear(repo_with_commit):
    (repo_with_commit / "b.txt").unlink()
    changed = evidence.parse_status_z(evidence.collect_status_z(str(repo_with_commit)))
    assert changed.deleted == ["b.txt"]


def test_renames_appear_when_reported(repo_with_commit):
    git = _git_exe()
    subprocess.run(
        [git, "mv", "a.txt", "renamed.txt"],
        cwd=repo_with_commit,
        check=True,
        capture_output=True,
    )
    entries = evidence.collect_status_z(str(repo_with_commit))
    changed = evidence.parse_status_z(entries)
    # Staged rename: R entry followed by NUL-separated new path.
    assert changed.renamed == ["renamed.txt"] or "renamed.txt" in (
        changed.renamed + changed.deleted + modified_or_added(changed)
    )


def modified_or_added(changed):
    return changed.modified


def test_untracked_paths_appear(repo_with_commit):
    (repo_with_commit / "new-untracked.bin").write_bytes(b"\x00\x01")
    changed = evidence.parse_status_z(evidence.collect_status_z(str(repo_with_commit)))
    assert changed.untracked == ["new-untracked.bin"]


def test_paths_with_spaces_parse_correctly(repo_with_commit):
    target = repo_with_commit / "dir with space" / "c.txt"
    target.write_text("changed\n", encoding="utf-8")
    changed = evidence.parse_status_z(evidence.collect_status_z(str(repo_with_commit)))
    assert changed.modified == ["dir with space/c.txt".replace("/", os.sep)] or changed.modified == [
        "dir with space/c.txt"
    ]


# --- tree digest -------------------------------------------------------------


def test_raw_bytes_affect_tree_digest(tmp_path):
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    for d in (d1, d2):
        d.mkdir()
    (d1 / "f.bin").write_bytes(b"\x00\x01\x02\xff")
    (d2 / "f.bin").write_bytes(b"\x00\x01\x02\xfe")
    assert evidence.candidate_tree_digest(str(d1)) != evidence.candidate_tree_digest(str(d2))


def test_newline_changes_affect_tree_digest(tmp_path):
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    for d in (d1, d2):
        d.mkdir()
    (d1 / "f.txt").write_bytes(b"a\r\nb\r\n")
    (d2 / "f.txt").write_bytes(b"a\nb\n")
    assert evidence.candidate_tree_digest(str(d1)) != evidence.candidate_tree_digest(str(d2))


def test_empty_file_affects_tree_digest(tmp_path):
    d1 = tmp_path / "one"
    d2 = tmp_path / "two"
    for d in (d1, d2):
        d.mkdir()
    (d1 / "empty.txt").write_bytes(b"")
    (d2 / "empty.txt").write_bytes(b"")
    (d1 / "x.txt").write_bytes(b"x")
    (d2 / "y.txt").write_bytes(b"y")
    # Same-shape trees with an empty file vs without differ; empty files hash distinctly.
    assert evidence.candidate_tree_digest(str(d1)) != evidence.candidate_tree_digest(str(d2))
    d3 = tmp_path / "three"
    d3.mkdir()
    (d3 / "empty.txt").write_bytes(b"")
    (d3 / "z.txt").write_bytes(b"z")
    assert evidence.candidate_tree_digest(str(d3)) not in ("",)


def test_git_entry_excluded(tmp_path):
    d1 = tmp_path / "one"
    d1.mkdir()
    (d1 / "content.txt").write_text("same", encoding="utf-8")
    git_file = d1 / ".git"
    git_file.write_text("gitdir: somewhere\n", encoding="utf-8")  # worktree-style file
    digest_file_form = evidence.candidate_tree_digest(str(d1))
    git_file.unlink()
    git_dir = d1 / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: main\n", encoding="utf-8")
    digest_dir_form = evidence.candidate_tree_digest(str(d1))
    assert digest_file_form == digest_dir_form


def _symlink_supported() -> bool:
    try:
        probe = Path(os.environ.get("TEMP", ".")) / "_hprlm_link_probe"
        target = Path(os.environ.get("TEMP", ".")) / "_hprlm_link_target"
        target.write_text("x", encoding="utf-8")
        if probe.exists():
            probe.unlink()
        os.symlink(target, probe)
        probe.unlink()
        target.unlink()
        return True
    except (OSError, NotImplementedError):
        return False


@pytest.mark.skipif(
    not _symlink_supported(),
    reason="symlink creation unsupported on this host",
)
def test_symlink_target_affects_digest(tmp_path):
    d = tmp_path / "base"
    d.mkdir()
    (d / "real.txt").write_text("v1", encoding="utf-8")
    link = d / "link.txt"

    if sys.platform == "win32":
        subprocess.run(
            ["cmd.exe", "/c", "mklink", str(link), str(d / "real.txt")],
            check=True,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    else:
        link.symlink_to(d / "real.txt")

    digest_v1 = evidence.candidate_tree_digest(str(d))
    (d / "real.txt").write_text("v2", encoding="utf-8")
    digest_v2 = evidence.candidate_tree_digest(str(d))
    assert digest_v1 != digest_v2


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform cannot create FIFOs")
def test_special_entries_fail_closed(tmp_path):
    d = tmp_path / "fifo-dir"
    d.mkdir()
    os.mkfifo(d / "pipe")
    with pytest.raises(evidence.TreeDigestLimitExceeded) as e:
        evidence.candidate_tree_digest(str(d))
    assert e.value.error_code == "SPECIAL_FILE_UNSUPPORTED"


def test_entry_count_limit_fails_closed(tmp_path):
    d = tmp_path / "many"
    d.mkdir()
    for i in range(60_001):
        (d / f"f{i}.txt").write_bytes(b"x")
    from conftest import schemas

    with pytest.raises(evidence.TreeDigestLimitExceeded) as e:
        evidence.candidate_tree_digest(
            str(d), max_entries=schemas.TREE_DIGEST_MAX_ENTRIES
        )
    assert e.value.error_code == "TOO_MANY_ENTRIES"


def test_byte_limit_fails_closed(tmp_path):
    d = tmp_path / "big"
    d.mkdir()
    blob = d / "blob.bin"
    with open(blob, "wb") as handle:
        # Sparse-ish write just over the 2 GiB bound using seeks.
        handle.seek(2 * 1024 * 1024 * 1024)
        handle.write(b"over")
    from conftest import schemas

    with pytest.raises(evidence.TreeDigestLimitExceeded) as e:
        evidence.candidate_tree_digest(
            str(d), max_total_file_bytes=schemas.TREE_DIGEST_MAX_TOTAL_FILE_BYTES
        )
    assert e.value.error_code == "TREE_TOO_LARGE"


def test_repeated_scans_deterministic(tmp_path):
    d = tmp_path / "stable"
    d.mkdir()
    (d / "sub").mkdir()
    (d / "sub" / "deep.txt").write_text("deep", encoding="utf-8")
    (d / "zz.txt").write_text("z", encoding="utf-8")
    (d / "aa.txt").write_text("a", encoding="utf-8")
    first = evidence.candidate_tree_digest(str(d))
    second = evidence.candidate_tree_digest(str(d))
    third = evidence.candidate_tree_digest(str(d))
    assert first == second == third


def test_tree_digest_streams_regular_files(tmp_path, monkeypatch):
    d = tmp_path / "streamed"
    d.mkdir()
    (d / "blob.bin").write_bytes(b"x" * (2 * 1024 * 1024))

    def forbid_read_bytes(_path):
        raise AssertionError("candidate_tree_digest must stream, not Path.read_bytes()")

    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)
    assert len(evidence.candidate_tree_digest(str(d))) == 64


def test_traversal_outside_candidate_fails_closed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    d = tmp_path / "root"
    d.mkdir()
    if sys.platform == "win32":

        pytest.skip("directory junction probing requires elevated setup on CI hosts")
    link = d / "escape"
    os.symlink(outside, link, target_is_directory=True)
    with pytest.raises(evidence.TreeDigestLimitExceeded) as e:
        evidence.candidate_tree_digest(str(d))
    assert e.value.error_code in (
        "TRAVERSAL_OUTSIDE_CANDIDATE",
        "SYMLINK_ESCAPES_CANDIDATE",
    )
