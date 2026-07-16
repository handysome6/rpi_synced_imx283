#!/usr/bin/env python3
"""Summarize timestamp deltas from capture and probe JSON files."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from pathlib import Path
from typing import Any, Iterable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, nargs="?", default=Path("captures"))
    parser.add_argument(
        "--include-not-ready",
        action="store_true",
        help="Include probe samples captured before both cameras reached SyncReady.",
    )
    return parser.parse_args()


def json_files(path: Path) -> Iterable[Path]:
    if path.is_file():
        yield path
    elif path.is_dir():
        yield from sorted(path.rglob("*.json"))


def extract_deltas_us(
    data: dict[str, Any], include_not_ready: bool = False
) -> list[float]:
    capture_delta = data.get("sensor_timestamp_delta_ns_client_minus_server")
    if capture_delta is not None:
        return [float(capture_delta) / 1000]
    rows = data.get("rows")
    if not isinstance(rows, list):
        return []
    deltas = []
    for row in rows:
        if not isinstance(row, dict) or row.get("delta_us_client_minus_server") is None:
            continue
        has_ready_fields = (
            "server_sync_ready" in row or "client_sync_ready" in row
        )
        both_ready = (
            row.get("server_sync_ready") is True
            and row.get("client_sync_ready") is True
        )
        if not include_not_ready and has_ready_fields and not both_ready:
            continue
        deltas.append(float(row["delta_us_client_minus_server"]))
    return deltas


def nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def main() -> int:
    args = parse_args()
    if not args.path.exists():
        print(f"ERROR: path does not exist: {args.path}")
        return 2

    deltas: list[float] = []
    parsed_files = 0
    skipped_files = 0
    for path in json_files(args.path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            file_deltas = extract_deltas_us(data, args.include_not_ready)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"WARN: skipped {path}: {exc}")
            skipped_files += 1
            continue
        if file_deltas:
            parsed_files += 1
            deltas.extend(file_deltas)

    if not deltas:
        print(f"No timestamp deltas found under {args.path}")
        return 1
    absolute = [abs(value) for value in deltas]
    print(f"path={args.path.resolve()}")
    print(f"include_not_ready={args.include_not_ready}")
    print(f"files_with_samples={parsed_files}")
    print(f"skipped_files={skipped_files}")
    print(f"sample_count={len(deltas)}")
    print(f"delta_us_min={min(deltas):.3f}")
    print(f"delta_us_max={max(deltas):.3f}")
    print(f"mean_absolute_delta_us={statistics.fmean(absolute):.3f}")
    print(f"p95_absolute_delta_us={nearest_rank(absolute, 0.95):.3f}")
    print(f"max_absolute_delta_us={max(absolute):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
