"""Receipt tests (spec §24 Receipt)."""

from __future__ import annotations

import json

from conftest import receipt as receipt_mod


def _sample_payload():
    from conftest import receipt_mod

    return {
        "schema_version": 1,
        "run_id": "00000000-0000-4000-8000-000000000000",
        "status": "VERIFIED",
        "automatic_retry_allowed": False,
        "limitations": receipt_mod.LIMITATIONS,
    }


def test_canonical_payload_bytes_deterministic():
    a = receipt_mod.canonical_payload_bytes({"b": 1, "a": {"z": 2, "y": [1, 2]}})
    b = receipt_mod.canonical_payload_bytes({"a": {"y": [1, 2], "z": 2}, "b": 1})
    assert a == b
    assert a.decode("utf-8").index('"a"') < a.decode("utf-8").index('"b"')


def test_receipt_digest_verifies(tmp_path):
    payload = _sample_payload()
    path = tmp_path / "receipt.json"
    digest = receipt_mod.write_receipt_atomic(payload, str(path))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["receipt_sha256"] == digest
    recomputed = receipt_mod.digest_of(envelope["receipt"])
    assert recomputed == digest


def test_one_byte_mutation_changes_digest():
    payload = _sample_payload()
    d1 = receipt_mod.digest_of(payload)
    mutated = dict(payload)
    mutated["status"] = "VERIFIEDX"
    d2 = receipt_mod.digest_of(mutated)
    assert d1 != d2


def test_atomic_replacement_never_exposes_partial_file(tmp_path):
    target = tmp_path / "receipt.json"
    payload = _sample_payload()
    receipt_mod.write_receipt_atomic(payload, str(target))
    body = target.read_bytes()
    # The final file must be complete, valid JSON (no torn write possible via replace).
    parsed = json.loads(body.decode("utf-8"))
    assert "receipt" in parsed and "receipt_sha256" in parsed
    # No temp files remain.
    leftovers = [p.name for p in tmp_path.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_no_environment_or_credentials_appear(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIME_RLM_RECEIPT_SECRET", "super-secret-value")
    payload = _sample_payload()
    path = tmp_path / "receipt.json"
    receipt_mod.write_receipt_atomic(payload, str(path))
    body = path.read_text(encoding="utf-8")
    assert "super-secret-value" not in body
    assert "PRIME_RLM_RECEIPT_SECRET" not in body
    assert "environ" not in body.lower().replace("environment", "")


def test_limitations_always_present():
    from conftest import receipt as r

    payload = _sample_payload()
    payload.pop("limitations")
    built = r.build_receipt(
        run_id="rid",
        status=r.Status.VERIFIED if hasattr(r, "Status") else None,
        request_sha256="0" * 64,
        prime_agent_version="0.8.2",
        platform_record={"os_name": "x", "sys_platform": "y", "python_version": "3"},
        repository_root="/repo",
        base_commit="1" * 40,
        candidate_path="/cand",
        candidate_head="1" * 40,
        candidate_tree_sha256="2" * 64,
        tracked_patch_sha256="3" * 64,
        prime_events_sha256="4" * 64,
        prime_stderr_sha256="5" * 64,
        changed_paths=r.ChangedPaths() if hasattr(r, "ChangedPaths") else _FakeChanged(),
        observation=_FakeObservation(),
        checks=[],
        started_at="2026-08-22T00:00:00.000Z",
        candidate_stability=r.CandidateStability.KNOWN,
    )
    assert built["limitations"] == r.LIMITATIONS
    assert built["automatic_retry_allowed"] is False


class _FakeChanged:
    def to_dict(self):
        return {"modified": [], "deleted": [], "renamed": [], "untracked": []}


class _FakeObservation:
    exit_code = 0
    session_id = "s"
    saw_agent_start = True
    saw_agent_end = True
    event_count = 3
    final_text = "text"
    timed_out = False
    stream_valid = True
    error_code = None


def test_platform_recorded():
    from conftest import receipt as r

    record = r.default_platform_record()
    assert set(record) == {"os_name", "sys_platform", "python_version"}
    assert record["os_name"]


def test_verified_requires_at_least_one_check():
    """Structural: the handler's status mapping enforces the rule."""
    from conftest import tools

    assert tools._map_status(had_checks=False, all_passed=True).value != "VERIFIED"
    assert tools._map_status(had_checks=True, all_passed=True).value == "VERIFIED"


def test_uncertain_records_unknown_stability(tmp_path):
    from conftest import receipt as r

    payload = r.build_receipt(
        run_id="rid",
        status=r.Status.UNCERTAIN,
        request_sha256="0" * 64,
        prime_agent_version="0.8.2",
        platform_record=r.default_platform_record(),
        repository_root="/repo",
        base_commit="1" * 40,
        candidate_path="/cand",
        candidate_head="1" * 40,
        candidate_tree_sha256="",
        tracked_patch_sha256="",
        prime_events_sha256="",
        prime_stderr_sha256="",
        changed_paths=r.ChangedPaths(),
        observation=_FakeObservation(),
        checks=[],
        started_at=r.utc_now_iso(),
        candidate_stability=r.CandidateStability.UNKNOWN,
    )
    assert payload["candidate_stability"] == "unknown"
    assert payload["candidate_may_have_partial_changes"] is True
    assert payload["automatic_retry_allowed"] is False
