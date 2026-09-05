"""Synthetic desktop/IPC overhead only; no Prime, provider, Git, or network."""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import platform
import statistics
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4


def _load():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "prime_desktop_benchmark", root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    return (
        importlib.import_module(f"{spec.name}.desktop"),
        importlib.import_module(f"{spec.name}.desktop_transport"),
    )


def _distribution(values: list[int]) -> dict:
    ordered = sorted(values)
    return {
        "median_us": round(statistics.median(ordered) / 1000, 3),
        "p95_us": round(ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)] / 1000, 3),
        "p99_us": round(ordered[max(0, (99 * len(ordered) + 99) // 100 - 1)] / 1000, 3),
    }


def benchmark(samples: int) -> dict:
    if not 1 <= samples <= 100_000:
        raise ValueError("samples must be between 1 and 100000")
    desktop, transport = _load()
    entered, release = threading.Event(), threading.Event()
    calls = 0

    def runner(_args, _ctx):
        nonlocal calls
        calls += 1
        entered.set()
        if not release.wait(60):
            raise RuntimeError("synthetic benchmark exceeded its safety timeout")
        return json.dumps({"status": "COMPLETED_UNVERIFIED", "checks": []})

    controller = desktop.DesktopRunController(object(), runner=runner)
    key = str(uuid4())
    snapshot_times, rpc_times = [], []
    try:
        started = time.perf_counter_ns()
        controller.submit(controller.session_id, key, {
            "goal": "Synthetic overhead measurement", "repository_path": "synthetic", "checks": [],
        })
        submit_us = (time.perf_counter_ns() - started) / 1000
        if not entered.wait(5):
            raise RuntimeError("synthetic worker did not start")
        frame = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "snapshot", "params": {
                "session_id": controller.session_id, "request_id": key,
            },
        }).encode()
        for _ in range(100):  # Warm up both code paths; excluded from samples.
            controller.snapshot(controller.session_id, key)
            transport.dispatch(controller, frame)
        largest = 0
        for _ in range(samples):
            started = time.perf_counter_ns()
            controller.snapshot(controller.session_id, key)
            snapshot_times.append(time.perf_counter_ns() - started)
            started = time.perf_counter_ns()
            response = transport.dispatch(controller, frame)
            rpc_times.append(time.perf_counter_ns() - started)
            largest = max(largest, len(response))
        return {
            "benchmark": "desktop-adapter-overhead-v1", "synthetic": True,
            "live_model_calls": 0, "worker_invocations": calls,
            "os": platform.system(), "python": platform.python_version(), "samples": samples,
            "submit_us_single_sample": round(submit_us, 3),
            "snapshot": _distribution(snapshot_times),
            "jsonrpc_snapshot": _distribution(rpc_times),
            "max_jsonrpc_snapshot_bytes": largest,
            "limitations": "Not inference speed, renderer FPS, RSS, or a before/after comparison.",
        }
    finally:
        release.set()
        controller.close(wait=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=2000)
    args = parser.parse_args()
    if not 1 <= args.samples <= 100_000:
        parser.error("--samples must be between 1 and 100000")
    print(json.dumps(benchmark(args.samples), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
