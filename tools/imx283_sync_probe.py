#!/usr/bin/env python3
"""Measure dual-camera software-sync offset over a sequence of frames."""

from __future__ import annotations

import argparse
import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-camera", type=int, default=0)
    parser.add_argument("--client-camera", type=int, default=1)
    parser.add_argument("--width", type=int, default=2736)
    parser.add_argument("--height", type=int, default=1824)
    parser.add_argument("--framerate", type=float, default=5.0)
    parser.add_argument("--frames", type=int, default=130)
    parser.add_argument("--buffer-count", type=int, default=6)
    parser.add_argument("--sync-frames", type=int, default=100)
    parser.add_argument("--request-timeout-s", type=float, default=20.0)
    parser.add_argument("--max-align-attempts", type=int, default=20)
    parser.add_argument("--exposure-us", type=int, default=10000)
    parser.add_argument("--gain", type=float, default=1.0)
    parser.add_argument("--quality", type=int, default=93)
    parser.add_argument("--output-dir", type=Path, default=Path("sync-probe"))
    parser.add_argument("--save-last", action="store_true")
    args = parser.parse_args()
    if args.server_camera == args.client_camera:
        parser.error("server and client camera numbers must differ")
    if args.framerate <= 0 or args.frames <= 0:
        parser.error("framerate and frames must be positive")
    if args.request_timeout_s <= 0 or args.max_align_attempts < 0:
        parser.error("timeout must be positive and attempts cannot be negative")
    return args


def import_camera_api() -> tuple[Any, Any]:
    try:
        from libcamera import controls
        from picamera2 import Picamera2
    except Exception as exc:  # pragma: no cover - requires Raspberry Pi runtime.
        raise SystemExit(f"Picamera2/libcamera is unavailable: {exc}") from exc
    return Picamera2, controls


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    return str(value)


def make_camera(
    Picamera2: Any,
    camera_number: int,
    role: str,
    sync_mode: Any,
    args: argparse.Namespace,
) -> Any:
    camera = Picamera2(camera_number)
    camera.options["quality"] = args.quality
    camera_controls = {
        "FrameRate": args.framerate,
        "AeEnable": False,
        "AwbEnable": False,
        "ExposureTime": args.exposure_us,
        "AnalogueGain": args.gain,
        "SyncMode": sync_mode,
    }
    if role == "server":
        camera_controls["SyncFrames"] = args.sync_frames
    config = camera.create_video_configuration(
        main={"size": (args.width, args.height), "format": "YUV420"},
        controls=camera_controls,
        buffer_count=args.buffer_count,
    )
    camera.configure(config)
    return camera


def wait_job(camera: Any, job: Any, timeout_s: float, description: str) -> Any:
    try:
        result = camera.wait(job, timeout=timeout_s)
    except TimeoutError as exc:
        raise TimeoutError(f"Timed out after {timeout_s:.1f}s: {description}") from exc
    if result is None:
        raise TimeoutError(f"Timed out after {timeout_s:.1f}s: {description}")
    return result


def next_request(camera: Any, timeout_s: float, description: str) -> Any:
    return wait_job(camera, camera.capture_request(wait=False), timeout_s, description)


def sensor_timestamp(request: Any) -> int:
    return int(request.get_metadata()["SensorTimestamp"])


def capture_matched_pair(
    server: Any,
    client: Any,
    frame_period_ns: int,
    timeout_s: float,
    max_align_attempts: int,
) -> tuple[Any, Any, int]:
    server_job = server.capture_request(wait=False)
    client_job = client.capture_request(wait=False)
    server_request = None
    client_request = None
    attempts = 0
    try:
        server_request = wait_job(server, server_job, timeout_s, "server frame")
        client_request = wait_job(client, client_job, timeout_s, "client frame")
        while True:
            delta_ns = sensor_timestamp(client_request) - sensor_timestamp(server_request)
            if abs(delta_ns) <= frame_period_ns // 2:
                return server_request, client_request, attempts
            if attempts >= max_align_attempts:
                raise RuntimeError(f"Could not match frame periods; delta_ns={delta_ns}")
            attempts += 1
            if delta_ns < 0:
                client_request.release()
                client_request = next_request(client, timeout_s, "next client frame")
            else:
                server_request.release()
                server_request = next_request(server, timeout_s, "next server frame")
    except Exception:
        if server_request is not None:
            server_request.release()
        if client_request is not None:
            client_request.release()
        raise


def build_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ready_rows = [
        row
        for row in rows
        if row["server_sync_ready"] is True and row["client_sync_ready"] is True
    ]
    deltas = [row["delta_us_client_minus_server"] for row in ready_rows]
    first_ready = ready_rows[0]["index"] if ready_rows else None
    return {
        "sample_count": len(rows),
        "ready_sample_count": len(ready_rows),
        "first_ready_index": first_ready,
        "ready_delta_us_min": min(deltas) if deltas else None,
        "ready_delta_us_max": max(deltas) if deltas else None,
        "ready_mean_absolute_delta_us": (
            statistics.fmean(abs(value) for value in deltas) if deltas else None
        ),
    }


def main() -> int:
    args = parse_args()
    Picamera2, controls = import_camera_api()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    frame_period_ns = int(round(1_000_000_000 / args.framerate))

    print("Detected cameras:")
    for info in Picamera2.global_camera_info():
        print(f"  {info}")
    server = make_camera(
        Picamera2,
        args.server_camera,
        "server",
        controls.rpi.SyncModeEnum.Server,
        args,
    )
    client = make_camera(
        Picamera2,
        args.client_camera,
        "client",
        controls.rpi.SyncModeEnum.Client,
        args,
    )

    rows: list[dict[str, Any]] = []
    last_server_request = None
    last_client_request = None
    started_client = False
    started_server = False
    try:
        print("Starting client first, then server...")
        client.start()
        started_client = True
        server.start()
        started_server = True
        for index in range(args.frames):
            if last_server_request is not None:
                last_server_request.release()
                last_client_request.release()
                last_server_request = None
                last_client_request = None
            server_request, client_request, attempts = capture_matched_pair(
                server,
                client,
                frame_period_ns,
                args.request_timeout_s,
                args.max_align_attempts,
            )
            server_metadata = dict(server_request.get_metadata())
            client_metadata = dict(client_request.get_metadata())
            delta_ns = (
                int(client_metadata["SensorTimestamp"])
                - int(server_metadata["SensorTimestamp"])
            )
            row = {
                "index": index,
                "delta_ns_client_minus_server": delta_ns,
                "delta_us_client_minus_server": delta_ns / 1000,
                "align_attempts": attempts,
                "server_sync_ready": server_metadata.get("SyncReady"),
                "client_sync_ready": client_metadata.get("SyncReady"),
                "server_sync_timer": server_metadata.get("SyncTimer"),
                "client_sync_timer": client_metadata.get("SyncTimer"),
                "server_metadata": jsonable(server_metadata),
                "client_metadata": jsonable(client_metadata),
            }
            rows.append(row)
            print(
                f"{index:04d} delta_us={row['delta_us_client_minus_server']:.3f} "
                f"align={attempts} ready=({row['server_sync_ready']},"
                f"{row['client_sync_ready']})",
                flush=True,
            )
            last_server_request = server_request
            last_client_request = client_request

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        json_path = args.output_dir / f"sync_probe_{stamp}.json"
        server_jpeg = None
        client_jpeg = None
        if args.save_last and last_server_request is not None:
            server_jpeg = args.output_dir / f"sync_probe_{stamp}_server.jpg"
            client_jpeg = args.output_dir / f"sync_probe_{stamp}_client.jpg"
            last_server_request.save("main", str(server_jpeg))
            last_client_request.save("main", str(client_jpeg))
        payload = {
            "created_utc": stamp,
            "configuration": vars(args) | {"output_dir": str(args.output_dir)},
            "frame_period_ns": frame_period_ns,
            "files": {
                "server": str(server_jpeg) if server_jpeg else None,
                "client": str(client_jpeg) if client_jpeg else None,
            },
            "summary": build_summary(rows),
            "rows": rows,
        }
        json_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        print(f"saved_json={json_path}")
        print(f"summary={json.dumps(payload['summary'], sort_keys=True)}")
    finally:
        if last_server_request is not None:
            last_server_request.release()
            last_client_request.release()
        print("Stopping cameras...")
        if started_server:
            server.stop()
        if started_client:
            client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
