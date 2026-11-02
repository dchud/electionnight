#!/usr/bin/env python3
"""Two-process lake soak test on a scratch lake.

A writer commits every 2 s while a reader queries every 0.5 s. Reports reader
errors, retries, and latency. Exits 1 on any unrecovered error.

    uv run python scripts/soak_lake.py --minutes 10
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import shutil
import statistics
import time
from pathlib import Path

import duckdb

from monitor.lake import BatchWriter, connect, init_lake
from monitor.settings import Paths, Settings

ROOT = Path("tmp/soak")
READ_RETRIES = 3


def scratch_settings() -> Settings:
    return Settings(paths=Paths.under(ROOT))


def writer_main(seconds: float, results: mp.Queue) -> None:
    settings = scratch_settings()
    conn = connect(settings)
    writer = BatchWriter(conn, settings)
    commits = 0
    errors = 0
    durations = []
    deadline = time.monotonic() + seconds
    index = 0
    while time.monotonic() < deadline:
        rows = [
            {
                "fetch_id": f"{index}-{n}",
                "source_id": "soak",
                "http_status": 200,
                "error": None,
            }
            for n in range(50)
        ]
        index += 1
        writer.add("raw.fetches", rows)
        started = time.monotonic()
        try:
            writer.flush()
            commits += 1
            durations.append(time.monotonic() - started)
        except duckdb.Error as error:
            errors += 1
            print("writer error:", str(error).splitlines()[0])
        time.sleep(2)
    conn.close()
    results.put(
        (
            "writer",
            {
                "commits": commits,
                "errors": errors,
                "p95_ms": _p95(durations),
            },
        )
    )


def reader_main(seconds: float, results: mp.Queue) -> None:
    settings = scratch_settings()
    conn = connect(settings, read_only=True)
    reads = 0
    retried = 0
    unrecovered = 0
    durations = []
    last = 0
    went_backwards = 0
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        started = time.monotonic()
        for attempt in range(READ_RETRIES):
            try:
                count = conn.execute(
                    "SELECT count(*) FROM lake.raw.fetches"
                ).fetchall()[0][0]
                conn.execute(
                    "SELECT max(snapshot_id) FROM lake.snapshots()"
                ).fetchall()
                reads += 1
                durations.append(time.monotonic() - started)
                if count < last:
                    went_backwards += 1
                last = count
                break
            except duckdb.Error as error:
                retried += 1
                if attempt == READ_RETRIES - 1:
                    unrecovered += 1
                    print("reader error:", str(error).splitlines()[0])
                time.sleep(0.2 * (attempt + 1))
        time.sleep(0.5)
    conn.close()
    results.put(
        (
            "reader",
            {
                "reads": reads,
                "retried": retried,
                "unrecovered": unrecovered,
                "went_backwards": went_backwards,
                "final_rows": last,
                "p95_ms": _p95(durations),
            },
        )
    )


def _p95(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    return round(statistics.quantiles(values, n=20)[18] * 1000, 1)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=10)
    args = parser.parse_args()
    shutil.rmtree(ROOT, ignore_errors=True)
    ROOT.mkdir(parents=True)
    init_lake(scratch_settings())
    seconds = args.minutes * 60
    results: mp.Queue = mp.Queue()
    processes = [
        mp.Process(target=writer_main, args=(seconds, results)),
        mp.Process(target=reader_main, args=(seconds, results)),
    ]
    for process in processes:
        process.start()
    outcome = dict(results.get() for _ in processes)
    for process in processes:
        process.join()
    print(outcome)
    failed = (
        outcome["writer"]["errors"]
        or outcome["reader"]["unrecovered"]
        or outcome["reader"]["went_backwards"]
    )
    print("soak FAILED" if failed else "soak passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
