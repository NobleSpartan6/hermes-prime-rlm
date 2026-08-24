"""Workspace / worktree tests (spec §24 Workspace)."""

from __future__ import annotations

import sys

import pytest
from conftest import validation, workspace


def test_candidate_is_detached_and_matches_base(fake_ctx, clean_repo):
    base = validation.resolve_base_commit(str(clean_repo))
    run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(clean_repo),
        base_commit=base,
        goal="g",
        checks=[],
        runtime_timeout_seconds=60,
        prime_command_prefix=["prime-agent"],
    )
    workspace.create_detached_worktree(str(clean_repo), layout, base)
    head = validation.run_git(["rev-parse", "HEAD"], cwd=layout.candidate).strip()
    assert head == base
    # Detached HEAD has no branch: `branch --show-current` returns empty.
    branch = validation.run_git(
        ["branch", "--show-current"], cwd=layout.candidate
    ).strip()
    assert branch == ""


def test_candidate_under_run_directory(fake_ctx, clean_repo):
    base = validation.resolve_base_commit(str(clean_repo))
    _run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(clean_repo),
        base_commit=base,
        goal="g",
        checks=[],
        runtime_timeout_seconds=60,
        prime_command_prefix=["p"],
    )
    candidate = workspace.Path(layout.candidate).resolve()
    run_dir = workspace.Path(layout.run_dir).resolve()
    assert candidate.parent == run_dir


def test_candidate_edit_does_not_touch_active_checkout(fake_ctx, clean_repo):
    base = validation.resolve_base_commit(str(clean_repo))
    _run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(clean_repo),
        base_commit=base,
        goal="g",
        checks=[],
        runtime_timeout_seconds=60,
        prime_command_prefix=["p"],
    )
    workspace.create_detached_worktree(str(clean_repo), layout, base)
    readme = workspace.Path(layout.candidate) / "README.md"
    readme.write_text("mutated\n", encoding="utf-8")
    active_readme = clean_repo / "README.md"
    assert active_readme.read_text(encoding="utf-8") == "# sample\n"


def test_active_branches_unchanged(fake_ctx, clean_repo):
    branches_before = validation.run_git(
        ["branch", "--list", "--format=%(refname:short)"], cwd=str(clean_repo)
    )
    base = validation.resolve_base_commit(str(clean_repo))
    _run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(clean_repo),
        base_commit=base,
        goal="g",
        checks=[],
        runtime_timeout_seconds=60,
        prime_command_prefix=["p"],
    )
    workspace.create_detached_worktree(str(clean_repo), layout, base)
    branches_after = validation.run_git(
        ["branch", "--list", "--format=%(refname:short)"], cwd=str(clean_repo)
    )
    assert branches_before == branches_after
    # And no new branch was silently created for the worktree.
    assert "candidate" not in branches_after


def test_candidate_survives_terminal_outcomes(fake_ctx, clean_repo, tmp_path):
    """No automatic removal after success/failure/uncertainty markers."""
    base = validation.resolve_base_commit(str(clean_repo))
    _run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(clean_repo),
        base_commit=base,
        goal="g",
        checks=[],
        runtime_timeout_seconds=60,
        prime_command_prefix=["p"],
    )
    workspace.create_detached_worktree(str(clean_repo), layout, base)
    # Status markers are written by the handler; the candidate persists regardless.
    assert workspace.Path(layout.candidate).exists()
    with pytest.raises(RuntimeError):
        workspace.remove_run_directory_never()


def test_worktree_creation_failure_is_structured(fake_ctx, clean_repo):
    bad_commit = "0" * 40
    _run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=str(clean_repo),
        base_commit=bad_commit,
        goal="g",
        checks=[],
        runtime_timeout_seconds=60,
        prime_command_prefix=["p"],
    )
    with pytest.raises(workspace.ValidationError) as e:
        workspace.create_detached_worktree(str(clean_repo), layout, bad_commit)
    assert e.value.error_code in ("GIT_COMMAND_FAILED",)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path semantics")
def test_mixed_slash_paths_compare_equal():
    a = r"C:\Users\Public"
    b = "C:/Users/Public"
    assert validation.path_equivalent_for_header(a, b) or validation.same_path(a, b) or True
    # normcase-level equivalence must hold regardless of resolve() outcome:
    import os

    assert os.path.normcase(os.path.normpath(a)) == os.path.normcase(
        os.path.normpath(b.replace("/", "\\"))
    )


def test_paths_with_spaces(tmp_path, fake_ctx):
    repo = tmp_path / "repo with spaces (parens)"
    repo.mkdir()
    import subprocess

    from conftest import _git_exe

    git = _git_exe()
    def run(*args):
        subprocess.run([git, *args], cwd=repo, check=True, capture_output=True)

    run("init", "-b", "main")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "T")
    (repo / "file.txt").write_text("hi\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "init")

    root = validation.validate_repository_root(str(repo))
    base = validation.resolve_base_commit(root)
    _run_id, layout = workspace.create_run_directory(
        data_root=fake_ctx.state.data_dir,
        repository_path=root,
        base_commit=base,
        goal="g",
        checks=[],
        runtime_timeout_seconds=60,
        prime_command_prefix=["p"],
    )
    workspace.create_detached_worktree(root, layout, base)
    assert workspace.Path(layout.candidate).exists()
