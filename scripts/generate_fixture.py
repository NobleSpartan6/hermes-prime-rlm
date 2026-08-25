#!/usr/bin/env python3
"""Generate the payments-api demo fixture: a buggy log analyzer + a huge log.

Creates (idempotently) in the target directory:
  app.py             -- error_rate() with the classic bug: it counts every
                        physical line as a log entry, so stack-trace
                        continuation lines pollute the rate.
  events.log         -- 84,600 physical lines / 60,000 entries + 24,600 trace
                        frames (~5.3 MB, ~1.5M tokens: far beyond any model's
                        context window).
  expected_rate.txt  -- ground-truth ERROR fraction over real entries only.
  run_tests.py       -- oracle test; must print PASS after the fix.
  README.md

The committed fixture is generated once and checked in so reviewers can run
the example without generating anything. This script exists to document
exactly how the data was made and to allow different sizes.

Usage:
    python generate_fixture.py [--dir examples/payments-api] [--seed 20260824]
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

# Entry mix measured from the committed fixture (kept stable for the oracle):
N_INFO, N_WARN, N_ERROR, N_DEBUG = 32_348, 6_397, 8_200, 13_055
TRACE_LINES_PER_ERROR = 3          # stack-trace frames under each ERROR entry
SERVICES = ["[fx]", "[auth]", "[gateway]", "[ledger]", "[notify]"]
FRAMES = [
    "at com.payments.gateway.Handler.handle(Handler.java:592)",
    "at com.payments.gateway.Router.dispatch(Router.java:144)",
    "at com.payments.ledger.Client.commit(Client.java:88)",
    "at org.apache.http.impl.execchain.MainClientExec.execute(MainClientExec.java:236)",
    "at java.base/java.lang.Thread.run(Thread.java:834)",
]


def build_lines() -> tuple[list[str], float]:
    rng = random.Random(20260824)
    lines: list[str] = []
    ts_ms = 0.0
    req = 0

    def next_ts() -> str:
        nonlocal ts_ms
        h = int(ts_ms // 3_600_000) % 24
        m = int(ts_ms // 60_000) % 60
        s = int(ts_ms // 1000) % 60
        ms = int(ts_ms) % 1000
        stamp = f"2026-08-24T{h:02d}:{m:02d}:{s:02d}.{ms:03d}Z"
        ts_ms += 4_041.7  # ~84,600 lines across one day
        return stamp

    entries: list[tuple[str, int]] = [
        ("INFO", N_INFO), ("WARN", N_WARN), ("ERROR", N_ERROR),
        # DEBUG entries exist in the log but are NOT part of the reference
        # entry population recorded in expected_rate.txt -- this is the trap
        # the agent must discover from the data itself.
        ("DEBUG", N_DEBUG),
    ]

    for level, count in entries:
        for _ in range(count):
            svc = rng.choice(SERVICES)
            latency = rng.randint(2, 990)
            lines.append(
                f"{next_ts()} {level:<5} {svc} req={req:06d} latency={latency}ms"
            )
            if level == "ERROR":
                for _ in range(TRACE_LINES_PER_ERROR):
                    frame = rng.choice(FRAMES)
                    lines.append(f"    {frame}")
            req += 1

    total_entries = N_INFO + N_WARN + N_ERROR          # DEBUG excluded
    return lines, N_ERROR / total_entries


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="examples/payments-api")
    args = ap.parse_args()

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    lines, expected = build_lines()
    (out / "events.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "expected_rate.txt").write_text(f"{expected:.6f}\n", encoding="utf-8")
    (out / "app.py").write_text(
        'def error_rate(path):\r\n'
        '    """Fraction of log entries that are ERROR level."""\r\n'
        "    total = 0\r\n"
        "    errs = 0\r\n"
        "    with open(path, encoding='utf-8') as fh:\r\n"
        "        for line in fh:\r\n"
        "            total += 1\r\n"
        "            if ' ERROR ' in line:\r\n"
        "                errs += 1\r\n"
        "    return errs / total\r\n",
        encoding="utf-8",
    )
    (out / "run_tests.py").write_text(
        "import sys\r\n"
        "from app import error_rate\r\n"
        "\r\n"
        "expected = float(open('expected_rate.txt').read().strip())\r\n"
        "got = error_rate('events.log')\r\n"
        "print(f'error_rate -> {got:.6f}, expected {expected:.6f}')\r\n"
        "assert abs(got - expected) < 0.0005, f'FAIL: {got} != {expected}'\r\n"
        "print('PASS')\r\n",
        encoding="utf-8",
    )
    (out / "README.md").write_text(
        "# payments-api log analyzer\n\n"
        "`app.py::error_rate` must return the fraction of log ENTRIES that are "
        "ERROR level. Continuation lines (stack-trace frames) are not entries.\n",
        encoding="utf-8",
    )
    print(f"wrote {len(lines)} lines to {out}/events.log")
    print(f"expected ERROR rate over real entries: {expected:.6f}")


if __name__ == "__main__":
    main()
