"""Input validation and version gate tests (spec §24 Validation)."""

from __future__ import annotations

import sys

import pytest
from conftest import schemas, validation


def _base_args(repo, **overrides):
    args = {
        "goal": "do a thing",
        "repository_path": str(repo),
        "checks": [],
        "runtime_timeout_seconds": 60,
    }
    args.update(overrides)
    return args


# --- goal -------------------------------------------------------------------


def test_empty_goal_rejected():
    with pytest.raises(schemas.ValidationError) as e:
        schemas.validate_goal("   ")
    assert e.value.error_code == "EMPTY_GOAL"


def test_oversized_goal_rejected():
    with pytest.raises(schemas.ValidationError):
        schemas.validate_goal("x" * (schemas.GOAL_MAX_CHARS + 1))


def test_nul_in_goal_rejected():
    with pytest.raises(schemas.ValidationError):
        schemas.validate_goal("bad\x00goal")


def test_goal_is_stripped():
    assert schemas.validate_goal("  hi  ") == "hi"


# --- repository path --------------------------------------------------------


def test_relative_repo_path_rejected(tmp_path):
    with pytest.raises(schemas.ValidationError) as e:
        schemas.validate_repository_path("relative/path")
    assert e.value.error_code == "RELATIVE_REPOSITORY_PATH"


def test_missing_repo_path_rejected():
    # Use a path that cannot exist on either platform (NUL is invalid everywhere).
    with pytest.raises(schemas.ValidationError) as e:
        schemas.validate_repository_path("/definitely/not/here-7f3d\x00")
    assert e.value.error_code in ("REPOSITORY_PATH_MISSING", "INVALID_REPOSITORY_PATH")


def test_non_git_directory_rejected(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(schemas.ValidationError) as e:
        validation.validate_repository_root(str(plain))
    assert e.value.error_code == "NOT_A_GIT_REPOSITORY"


def test_nested_subdirectory_rejected(clean_repo):
    nested = clean_repo / "sub"
    nested.mkdir()
    with pytest.raises(schemas.ValidationError) as e:
        validation.validate_repository_root(str(nested))
    assert e.value.error_code == "NESTED_REPOSITORY_SUBDIRECTORY"


def test_dirty_tracked_repository_rejected(clean_repo):
    (clean_repo / "src.py").write_text("print('changed')\n", encoding="utf-8")
    with pytest.raises(schemas.ValidationError) as e:
        validation.require_clean_repository(str(clean_repo))
    assert e.value.error_code == "DIRTY_REPOSITORY"


def test_untracked_file_rejected(clean_repo):
    (clean_repo / "stray.txt").write_text("untracked\n", encoding="utf-8")
    with pytest.raises(schemas.ValidationError) as e:
        validation.require_clean_repository(str(clean_repo))
    assert e.value.error_code == "DIRTY_REPOSITORY"


def test_submodule_repository_rejected(clean_repo):
    (clean_repo / ".gitmodules").write_text(
        '[submodule "x"]\npath=x\nurl=https://example.invalid\n', encoding="utf-8"
    )
    with pytest.raises(schemas.ValidationError) as e:
        validation.reject_submodules(str(clean_repo))
    assert e.value.error_code == "SUBMODULES_UNSUPPORTED"


# --- checks -----------------------------------------------------------------


def test_invalid_check_name_rejected():
    with pytest.raises(schemas.ValidationError):
        schemas.validate_check_name("bad name!")


def test_empty_argv_rejected():
    with pytest.raises(schemas.ValidationError):
        schemas.validate_check_argv([])


def test_oversized_argv_entry_rejected():
    with pytest.raises(schemas.ValidationError):
        schemas.validate_check_argv(["x" * (schemas.CHECK_ARGV_ENTRY_MAX_CHARS + 1)])


def test_too_many_checks_rejected():
    checks = [
        {"name": f"c{i}", "argv": ["python", "-c", "1"]} for i in range(schemas.CHECKS_MAX_ENTRIES + 1)
    ]
    with pytest.raises(schemas.ValidationError):
        schemas.validate_checks(checks)


def test_duplicate_check_names_rejected():
    dup = [{"name": "same", "argv": ["a"]}, {"name": "same", "argv": ["b"]}]
    with pytest.raises(schemas.ValidationError):
        schemas.validate_checks(dup)


@pytest.mark.parametrize("bad", [0, -1, 1801, "300", True])
def test_invalid_check_timeout_rejected(bad):
    with pytest.raises(schemas.ValidationError):
        schemas.validate_check_timeout(bad)


def test_check_timeout_default_applied():
    assert schemas.validate_check_timeout(None) == schemas.CHECK_TIMEOUT_DEFAULT_SECONDS


# --- runtime timeout ---------------------------------------------------------


@pytest.mark.parametrize("bad", [29, 3601, "1200"])
def test_invalid_runtime_timeout_rejected(bad):
    with pytest.raises(schemas.ValidationError):
        schemas.validate_runtime_timeout(bad)


def test_runtime_timeout_bounds_accepted():
    assert schemas.validate_runtime_timeout(30) == 30
    assert schemas.validate_runtime_timeout(3600) == 3600
    assert (
        schemas.validate_runtime_timeout(None) == schemas.RUNTIME_TIMEOUT_DEFAULT_SECONDS
    )


# --- prime version gate -------------------------------------------------------


@pytest.mark.parametrize("version", ["0.8.1", "prime-agent 0.8.1", "v0.8.1"])
def test_exact_prime_version_accepted(version):
    assert schemas.check_prime_version_compatible(version) == (0, 8, 1)


@pytest.mark.parametrize(
    "version", ["0.7.4", "0.8.0", "0.8.2", "0.8.99", "0.9.0", "1.0.0"]
)
def test_other_prime_versions_rejected(version):
    with pytest.raises(schemas.ValidationError) as e:
        schemas.check_prime_version_compatible(version)
    assert e.value.error_code == "UNSUPPORTED_PRIME_VERSION"


@pytest.mark.parametrize("garbage", ["", "not-a-version", "2026.08.22", "1.2.3.4.5"])
def test_malformed_version_rejected(garbage):
    with pytest.raises(schemas.ValidationError):
        schemas.extract_semver(garbage)


def test_two_version_strings_rejected():
    with pytest.raises(schemas.ValidationError):
        schemas.extract_semver("compatible with 0.8.0 through 0.9.4")


def test_noisy_prime_version_probe_fails_at_output_bound(tmp_path):
    script = tmp_path / "noisy_version.py"
    script.write_text("import sys\nsys.stdout.write('x' * 200000)\n", encoding="utf-8")
    with pytest.raises(schemas.ValidationError) as exc:
        validation.probe_prime_version([sys.executable, str(script)])
    assert exc.value.error_code == "PRIME_VERSION_OUTPUT_LIMIT"


def test_prime_version_probe_excludes_ambient_provider_credentials(tmp_path, monkeypatch):
    script = tmp_path / "version_env_probe.py"
    script.write_text(
        "import os\n"
        "if os.getenv('OPENAI_API_KEY'):\n"
        "    raise SystemExit(7)\n"
        "print('prime-agent 0.8.1')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-prime")

    version, text = validation.probe_prime_version([sys.executable, str(script)])

    assert version == (0, 8, 1)
    assert text == "prime-agent 0.8.1"


def test_git_capture_and_file_output_fail_closed_at_bounds(clean_repo, tmp_path):
    for index in range(20):
        (clean_repo / f"untracked-{index:03d}.txt").write_text("x", encoding="utf-8")
    with pytest.raises(schemas.ValidationError) as exc:
        validation.run_git(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            cwd=str(clean_repo),
            max_stdout_bytes=100,
        )
    assert exc.value.error_code == "GIT_OUTPUT_LIMIT"

    tracked = clean_repo / "src.py"
    tracked.write_text("x" * 10_000, encoding="utf-8")
    with pytest.raises(schemas.ValidationError) as exc:
        validation.run_git_to_file(
            ["diff", "--binary", "--full-index", "HEAD", "--"],
            cwd=str(clean_repo),
            output_path=str(tmp_path / "tracked.patch"),
            max_stdout_bytes=100,
        )
    assert exc.value.error_code == "GIT_OUTPUT_LIMIT"


# --- admission leaves no trace ------------------------------------------------


def test_validation_failure_leaves_no_run_directory(fake_ctx, clean_repo):
    """A dirty repo rejected at admission must create no run directory."""
    from conftest import tools as tools_mod

    (clean_repo / "dirt.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(schemas.ValidationError) as e:
        tools_mod._run(
            {
                "goal": "g",
                "repository_path": str(clean_repo),
                "checks": [],
            },
            fake_ctx,
        )
    assert e.value.error_code == "DIRTY_REPOSITORY"
    runs = list((fake_ctx.state.data_dir).glob("runs/*"))
    assert runs == []
