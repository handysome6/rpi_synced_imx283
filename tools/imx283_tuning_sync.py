#!/usr/bin/env python3
"""Check, enable, or restore rpi.sync in the PiSP IMX283 tuning file."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_TUNING_FILE = Path("/usr/share/libcamera/ipa/rpi/pisp/imx283.json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tuning-file", type=Path, default=DEFAULT_TUNING_FILE)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="Only report current state.")
    action.add_argument(
        "--apply", action="store_true", help="Add rpi.sync after creating a backup."
    )
    action.add_argument(
        "--restore", type=Path, metavar="BACKUP", help="Restore a validated backup file."
    )
    return parser.parse_args()


def load_tuning(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as strict_error:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError(
                "The tuning file uses libcamera's relaxed JSON syntax. Install "
                "python3-yaml so it can be parsed safely."
            ) from exc
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise ValueError(
                f"Cannot parse libcamera tuning syntax: {strict_error}"
            ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"Tuning root must be an object: {path}")
    algorithms = data.get("algorithms")
    if not isinstance(algorithms, list):
        raise ValueError(f"Tuning file has no algorithms array: {path}")
    return data


def has_rpi_sync(data: dict[str, Any]) -> bool:
    return any(
        isinstance(algorithm, dict) and "rpi.sync" in algorithm
        for algorithm in data["algorithms"]
    )


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    source_stat = path.stat()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            json.dump(data, stream, indent=4)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, source_stat.st_mode)
        if os.geteuid() == 0:
            os.chown(temporary_path, source_stat.st_uid, source_stat.st_gid)
        load_tuning(temporary_path)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def atomic_restore(path: Path, backup: Path) -> None:
    load_tuning(backup)
    source_stat = path.stat()
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            with backup.open("rb") as source:
                shutil.copyfileobj(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, source_stat.st_mode)
        if os.geteuid() == 0:
            os.chown(temporary_path, source_stat.st_uid, source_stat.st_gid)
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def main() -> int:
    args = parse_args()
    tuning_file = args.tuning_file.resolve()
    print(f"tuning_file={tuning_file}")

    if args.restore is not None:
        try:
            atomic_restore(tuning_file, args.restore.resolve())
            restored = load_tuning(tuning_file)
        except (OSError, ValueError) as exc:
            print(f"ERROR: restore failed: {exc}")
            return 2
        print(f"restored_from={args.restore.resolve()}")
        print(f"rpi.sync={'enabled' if has_rpi_sync(restored) else 'missing'}")
        return 0

    try:
        data = load_tuning(tuning_file)
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read tuning file: {exc}")
        return 2

    enabled = has_rpi_sync(data)
    print(f"rpi.sync={'enabled' if enabled else 'missing'}")

    if not args.apply:
        return 0 if enabled else 1
    if enabled:
        print("No change required.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = tuning_file.with_name(f"{tuning_file.name}.before-rpi-sync-{stamp}")
    try:
        shutil.copy2(tuning_file, backup)
        data["algorithms"].append({"rpi.sync": {}})
        atomic_write_json(tuning_file, data)
    except (OSError, ValueError) as exc:
        print(f"ERROR: apply failed: {exc}")
        return 2

    print(f"backup={backup}")
    print("rpi.sync=enabled")
    print("Restart camera applications so the updated tuning file is reloaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
