#!/usr/bin/env python3
"""Run read-only checks for the tested Raspberry Pi 5 dual-IMX283 setup."""

from __future__ import annotations

import argparse
import json
import platform
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--boot-config", type=Path, default=Path("/boot/firmware/config.txt")
    )
    parser.add_argument(
        "--tuning-file",
        type=Path,
        default=Path("/usr/share/libcamera/ipa/rpi/pisp/imx283.json"),
    )
    parser.add_argument(
        "--strict", action="store_true", help="Return nonzero when a required check fails."
    )
    return parser.parse_args()


def active_config_lines(path: Path) -> list[str]:
    lines = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def tuning_has_sync(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as strict_error:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError(
                "Relaxed tuning JSON requires the python3-yaml package"
            ) from exc
        try:
            data = yaml.safe_load(text)
        except Exception as exc:
            raise ValueError(
                f"Cannot parse libcamera tuning syntax: {strict_error}"
            ) from exc
    if not isinstance(data, dict):
        raise ValueError("Tuning root is not an object")
    algorithms = data.get("algorithms", [])
    return any(
        isinstance(algorithm, dict) and "rpi.sync" in algorithm
        for algorithm in algorithms
    )


def print_check(name: str, passed: bool, detail: Any) -> None:
    print(f"[{'PASS' if passed else 'FAIL'}] {name}: {detail}")


def main() -> int:
    args = parse_args()
    results: list[bool] = []
    print(f"platform={platform.platform()}")

    try:
        lines = active_config_lines(args.boot_config)
        auto_detect_off = "camera_auto_detect=0" in lines
        overlays = [line for line in lines if line.startswith("dtoverlay=imx283")]
        dual_overlays = "dtoverlay=imx283,cam0" in overlays and any(
            line == "dtoverlay=imx283" for line in overlays
        )
        print_check("camera auto-detection disabled", auto_detect_off, args.boot_config)
        print_check("dual IMX283 overlays", dual_overlays, overlays)
        results.extend((auto_detect_off, dual_overlays))
    except OSError as exc:
        print_check("boot configuration readable", False, exc)
        results.extend((False, False))

    try:
        sync_enabled = tuning_has_sync(args.tuning_file)
        print_check("PiSP IMX283 rpi.sync", sync_enabled, args.tuning_file)
        results.append(sync_enabled)
    except (OSError, ValueError) as exc:
        print_check("PiSP tuning file readable", False, exc)
        results.append(False)

    try:
        from libcamera import controls
        from picamera2 import Picamera2

        api_checks = {
            "SyncModeEnum": hasattr(controls.rpi, "SyncModeEnum"),
            "SyncReady": hasattr(controls.rpi, "SyncReady"),
            "SyncTimer": hasattr(controls.rpi, "SyncTimer"),
            "SyncFrames": hasattr(controls.rpi, "SyncFrames"),
            "capture_sync_request": hasattr(Picamera2, "capture_sync_request"),
            "wait": hasattr(Picamera2, "wait"),
        }
        api_ok = all(api_checks.values())
        print_check("Picamera2 sync API", api_ok, api_checks)
        results.append(api_ok)

        camera_info = Picamera2.global_camera_info()
        imx283_cameras = [
            info
            for info in camera_info
            if "imx283" in str(info.get("Model", "")).lower()
        ]
        cameras_ok = len(imx283_cameras) >= 2
        print_check("two IMX283 cameras", cameras_ok, imx283_cameras)
        results.append(cameras_ok)
    except Exception as exc:
        print_check("Picamera2 import and enumeration", False, exc)
        results.extend((False, False))

    passed = all(results)
    print(f"overall={'PASS' if passed else 'FAIL'}")
    return 0 if passed or not args.strict else 2


if __name__ == "__main__":
    raise SystemExit(main())
