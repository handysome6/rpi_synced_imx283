#!/usr/bin/env python3
"""Capture synchronized JPEG pairs across a manual exposure-time sweep."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import dual_imx283_sync_capture as sync_capture  # noqa: E402


def exposure_list(value: str) -> list[int]:
    try:
        exposures = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("exposures must be comma-separated integers") from exc
    if not exposures or any(exposure <= 0 for exposure in exposures):
        raise argparse.ArgumentTypeError("exposures must contain positive integers")
    return exposures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-camera", type=int, default=0)
    parser.add_argument("--client-camera", type=int, default=1)
    parser.add_argument("--width", type=int, default=2736)
    parser.add_argument("--height", type=int, default=1824)
    parser.add_argument("--format", default="YUV420")
    parser.add_argument("--framerate", type=float, default=5.0)
    parser.add_argument("--buffer-count", type=int, default=6)
    parser.add_argument("--sync-frames", type=int, default=100)
    parser.add_argument("--sync-timeout-s", type=float, default=90.0)
    parser.add_argument("--request-timeout-s", type=float, default=20.0)
    parser.add_argument("--max-align-attempts", type=int, default=20)
    parser.add_argument(
        "--exposures-us",
        type=exposure_list,
        default=exposure_list("100,200,400,800,1200,1600,2400,3200,4800,6400,8000"),
    )
    parser.add_argument(
        "--gain",
        type=float,
        help="Analogue gain. The common maximum reported by both cameras is used by default.",
    )
    parser.add_argument("--settle-frames", type=int, default=3)
    parser.add_argument("--control-wait-frames", type=int, default=20)
    parser.add_argument("--quality", type=int, default=95)
    parser.add_argument("--output-dir", type=Path, default=Path("exposure-sweep"))
    parser.add_argument("--prefix", default="clock_sweep")
    args = parser.parse_args()
    if args.server_camera == args.client_camera:
        parser.error("server and client camera numbers must differ")
    if (
        args.framerate <= 0
        or args.settle_frames < 1
        or args.control_wait_frames < args.settle_frames
    ):
        parser.error(
            "framerate and settle-frames must be positive, and control-wait-frames "
            "must be at least settle-frames"
        )
    if not 1 <= args.quality <= 100:
        parser.error("quality must be between 1 and 100")
    return args


def common_control_range(
    server: Any, client: Any, control_name: str
) -> tuple[float, float]:
    server_range = server.camera_controls.get(control_name)
    client_range = client.camera_controls.get(control_name)
    if server_range is None or client_range is None:
        raise RuntimeError(f"Both cameras must expose {control_name}")
    minimum = max(float(server_range[0]), float(client_range[0]))
    maximum = min(float(server_range[1]), float(client_range[1]))
    if minimum > maximum:
        raise RuntimeError(f"The cameras have no common {control_name} range")
    return minimum, maximum


def release_pair(requests: tuple[Any, Any]) -> None:
    requests[0].release()
    requests[1].release()


def settle_controls(
    server: Any,
    client: Any,
    exposure_us: int,
    gain: float,
    settle_frames: int,
    max_wait_frames: int,
    timeout_s: float,
) -> tuple[dict[str, Any], dict[str, Any]]:
    controls = {
        "AeEnable": False,
        "AwbEnable": False,
        "ExposureTime": exposure_us,
        "AnalogueGain": gain,
    }
    server.set_controls(controls)
    client.set_controls(controls)
    flush_timestamp_ns = time.monotonic_ns()
    server_metadata: dict[str, Any] = {}
    client_metadata: dict[str, Any] = {}
    consecutive_matching_frames = 0
    tolerance_us = max(20, int(round(exposure_us * 0.01)))
    for attempt in range(max_wait_frames):
        flush = flush_timestamp_ns if attempt == 0 else None
        server_job = server.capture_request(wait=False, flush=flush)
        client_job = client.capture_request(wait=False, flush=flush)
        server_request = sync_capture.wait_job(
            server, server_job, timeout_s, "settling server exposure controls"
        )
        client_request = None
        try:
            client_request = sync_capture.wait_job(
                client, client_job, timeout_s, "settling client exposure controls"
            )
            server_metadata = dict(server_request.get_metadata())
            client_metadata = dict(client_request.get_metadata())
        finally:
            server_request.release()
            if client_request is not None:
                client_request.release()
        server_exposure = int(server_metadata.get("ExposureTime", -1))
        client_exposure = int(client_metadata.get("ExposureTime", -1))
        exposure_matches = (
            abs(server_exposure - exposure_us) <= tolerance_us
            and abs(client_exposure - exposure_us) <= tolerance_us
        )
        consecutive_matching_frames = (
            consecutive_matching_frames + 1 if exposure_matches else 0
        )
        if consecutive_matching_frames >= settle_frames:
            return server_metadata, client_metadata
    raise TimeoutError(
        f"Exposure {exposure_us}us did not settle within {max_wait_frames} frames; "
        f"last metadata=({server_metadata.get('ExposureTime')},"
        f"{client_metadata.get('ExposureTime')})us"
    )


def main() -> int:
    args = parse_args()
    Picamera2, controls = sync_capture.import_camera_api()
    server = Picamera2(args.server_camera)
    client = Picamera2(args.client_camera)
    server.options["quality"] = args.quality
    client.options["quality"] = args.quality

    exposure_min, exposure_max = common_control_range(server, client, "ExposureTime")
    gain_min, gain_max = common_control_range(server, client, "AnalogueGain")
    gain = gain_max if args.gain is None else args.gain
    if not gain_min <= gain <= gain_max:
        raise SystemExit(f"Gain {gain} is outside the common range {gain_min}..{gain_max}")
    invalid_exposures = [
        exposure
        for exposure in args.exposures_us
        if not exposure_min <= exposure <= exposure_max
    ]
    if invalid_exposures:
        raise SystemExit(
            f"Exposure values outside {exposure_min}..{exposure_max}: {invalid_exposures}"
        )

    args.manual = True
    args.exposure_us = args.exposures_us[0]
    args.gain = gain
    args.output_dir.mkdir(parents=True, exist_ok=True)
    server.configure(sync_capture.configure_camera(server, args, controls, "server"))
    client.configure(sync_capture.configure_camera(client, args, controls, "client"))

    started_client = False
    started_server = False
    sync_requests = None
    manifest_rows: list[dict[str, Any]] = []
    try:
        print(
            f"Common controls: ExposureTime={exposure_min:.0f}..{exposure_max:.0f}us "
            f"AnalogueGain={gain_min:.3f}..{gain_max:.3f}; using gain={gain:.3f}",
            flush=True,
        )
        print("Starting client first, then server...", flush=True)
        client.start()
        started_client = True
        server.start()
        started_server = True

        print("Waiting for both cameras to reach SyncReady...", flush=True)
        sync_requests = sync_capture.wait_for_sync_pair(
            server, client, args.sync_timeout_s
        )
        sync_capture.print_sync_request("server", sync_requests[0])
        sync_capture.print_sync_request("client", sync_requests[1])
        release_pair(sync_requests)
        sync_requests = None

        frame_period_ns = int(round(1_000_000_000 / args.framerate))
        base_prefix = args.prefix
        for index, exposure_us in enumerate(args.exposures_us, start=1):
            print(f"Applying exposure {exposure_us}us...", flush=True)
            settled_server, settled_client = settle_controls(
                server,
                client,
                exposure_us,
                gain,
                args.settle_frames,
                args.control_wait_frames,
                args.request_timeout_s,
            )
            args.exposure_us = exposure_us
            args.prefix = f"{base_prefix}_{exposure_us:06d}us"
            record = sync_capture.capture_pair(
                server,
                client,
                args,
                index,
                time.monotonic_ns(),
                frame_period_ns,
            )
            row = {
                "requested_exposure_us": exposure_us,
                "requested_gain": gain,
                "settled_server_exposure_us": settled_server.get("ExposureTime"),
                "settled_client_exposure_us": settled_client.get("ExposureTime"),
                "captured_server_exposure_us": record["server_metadata"].get(
                    "ExposureTime"
                ),
                "captured_client_exposure_us": record["client_metadata"].get(
                    "ExposureTime"
                ),
                "captured_server_gain": record["server_metadata"].get("AnalogueGain"),
                "captured_client_gain": record["client_metadata"].get("AnalogueGain"),
                "sensor_delta_ns_client_minus_server": record[
                    "sensor_timestamp_delta_ns_client_minus_server"
                ],
                "files": record["files"],
                "metadata_file": record["metadata_file"],
            }
            manifest_rows.append(row)
            print(
                f"Saved exposure {exposure_us}us: actual=({row['captured_server_exposure_us']},"
                f"{row['captured_client_exposure_us']})us "
                f"delta_ns={row['sensor_delta_ns_client_minus_server']}",
                flush=True,
            )

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        manifest = args.output_dir / f"{base_prefix}_manifest_{stamp}.json"
        manifest.write_text(
            json.dumps(
                {
                    "created_utc": stamp,
                    "server_camera": args.server_camera,
                    "client_camera": args.client_camera,
                    "resolution": [args.width, args.height],
                    "framerate": args.framerate,
                    "gain": gain,
                    "rows": manifest_rows,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"saved_manifest={manifest}", flush=True)
    finally:
        if sync_requests is not None:
            release_pair(sync_requests)
        print("Stopping cameras...", flush=True)
        if started_server:
            server.stop()
        if started_client:
            client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
