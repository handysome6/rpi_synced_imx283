#!/usr/bin/env python3
"""Capture software-synchronized JPEG pairs from two Raspberry Pi cameras."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture software-synchronized JPEG pairs from two cameras."
    )
    parser.add_argument("--server-camera", type=int, default=0)
    parser.add_argument("--client-camera", type=int, default=1)
    parser.add_argument("--width", type=int, default=2736)
    parser.add_argument("--height", type=int, default=1824)
    parser.add_argument(
        "--format",
        choices=("YUV420", "RGB888", "XBGR8888", "XRGB8888"),
        default="YUV420",
    )
    parser.add_argument("--framerate", type=float, default=5.0)
    parser.add_argument("--buffer-count", type=int, default=6)
    parser.add_argument("--sync-frames", type=int, default=100)
    parser.add_argument("--sync-timeout-s", type=float, default=90.0)
    parser.add_argument("--request-timeout-s", type=float, default=20.0)
    parser.add_argument("--output-dir", type=Path, default=Path("captures"))
    parser.add_argument("--prefix", default="imx283_sync")
    parser.add_argument("--quality", type=int, default=93)
    parser.add_argument(
        "--max-align-attempts",
        type=int,
        default=20,
        help="Maximum extra requests used to match frames from the same frame period.",
    )
    parser.add_argument(
        "--trigger",
        choices=("sigusr1", "stdin", "once"),
        default="sigusr1",
        help="Capture on SIGUSR1, Enter, or once immediately after sync is ready.",
    )
    parser.add_argument(
        "--manual",
        action="store_true",
        help="Disable AE/AWB for repeatable stereo pairs.",
    )
    parser.add_argument("--exposure-us", type=int)
    parser.add_argument("--gain", type=float)
    parser.add_argument(
        "--max-captures",
        type=int,
        default=0,
        help="Stop after this many pairs; 0 runs until interrupted.",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Check camera enumeration and sync API availability without streaming.",
    )
    args = parser.parse_args()

    if args.server_camera == args.client_camera:
        parser.error("server and client camera numbers must differ")
    if args.width <= 0 or args.height <= 0:
        parser.error("width and height must be positive")
    if args.framerate <= 0:
        parser.error("framerate must be positive")
    if not 1 <= args.quality <= 100:
        parser.error("quality must be between 1 and 100")
    if args.sync_frames <= 0:
        parser.error("sync-frames must be positive")
    if args.sync_timeout_s <= 0 or args.request_timeout_s <= 0:
        parser.error("timeouts must be positive")
    if args.max_align_attempts < 0 or args.max_captures < 0:
        parser.error("attempt and capture limits cannot be negative")
    if args.exposure_us is not None and not args.manual:
        parser.error("--exposure-us requires --manual")
    if args.gain is not None and not args.manual:
        parser.error("--gain requires --manual")
    return args


def import_camera_api() -> tuple[Any, Any]:
    try:
        from libcamera import controls
        from picamera2 import Picamera2
    except Exception as exc:  # pragma: no cover - requires Raspberry Pi runtime.
        raise SystemExit(
            "Picamera2/libcamera is unavailable. Run this on Raspberry Pi OS with "
            f"python3-picamera2 installed. Import error: {exc}"
        ) from exc
    return Picamera2, controls


def jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    return str(value)


def exposure_start_ns(metadata: dict[str, Any]) -> int | None:
    sensor_timestamp = metadata.get("SensorTimestamp")
    exposure_us = metadata.get("ExposureTime")
    if sensor_timestamp is None or exposure_us is None:
        return None
    return int(sensor_timestamp) - int(exposure_us) * 1000


def build_controls(args: argparse.Namespace, controls: Any, role: str) -> dict[str, Any]:
    sync_mode = (
        controls.rpi.SyncModeEnum.Server
        if role == "server"
        else controls.rpi.SyncModeEnum.Client
    )
    camera_controls: dict[str, Any] = {
        "FrameRate": args.framerate,
        "SyncMode": sync_mode,
    }
    if role == "server":
        camera_controls["SyncFrames"] = args.sync_frames
    if args.manual:
        camera_controls.update({"AeEnable": False, "AwbEnable": False})
        if args.exposure_us is not None:
            camera_controls["ExposureTime"] = args.exposure_us
        if args.gain is not None:
            camera_controls["AnalogueGain"] = args.gain
    return camera_controls


def configure_camera(
    camera: Any, args: argparse.Namespace, controls: Any, role: str
) -> Any:
    return camera.create_video_configuration(
        main={"size": (args.width, args.height), "format": args.format},
        controls=build_controls(args, controls, role),
        buffer_count=args.buffer_count,
    )


def wait_job(camera: Any, job: Any, timeout_s: float, description: str) -> Any:
    try:
        result = camera.wait(job, timeout=timeout_s)
    except TimeoutError as exc:
        raise TimeoutError(f"Timed out after {timeout_s:.1f}s: {description}") from exc
    if result is None:
        raise TimeoutError(f"Timed out after {timeout_s:.1f}s: {description}")
    return result


def capture_request(
    camera: Any,
    timeout_s: float,
    description: str,
    flush: int | None = None,
) -> Any:
    job = camera.capture_request(wait=False, flush=flush)
    return wait_job(camera, job, timeout_s, description)


def wait_for_sync_pair(
    server: Any, client: Any, timeout_s: float
) -> tuple[Any, Any]:
    deadline = time.monotonic() + timeout_s
    server_job = server.capture_sync_request(wait=False)
    client_job = client.capture_sync_request(wait=False)
    server_request = None
    try:
        server_request = wait_job(
            server,
            server_job,
            max(0.001, deadline - time.monotonic()),
            "waiting for server SyncReady",
        )
        client_request = wait_job(
            client,
            client_job,
            max(0.001, deadline - time.monotonic()),
            "waiting for client SyncReady",
        )
        return server_request, client_request
    except Exception:
        if server_request is not None:
            server_request.release()
        raise


def get_sensor_timestamp(request: Any, role: str) -> int:
    metadata = request.get_metadata()
    try:
        return int(metadata["SensorTimestamp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{role} request has no valid SensorTimestamp") from exc


def capture_matched_requests(
    server: Any,
    client: Any,
    trigger_monotonic_ns: int,
    request_timeout_s: float,
    frame_period_ns: int,
    max_align_attempts: int,
) -> tuple[Any, Any, int, int]:
    server_job = server.capture_request(wait=False, flush=trigger_monotonic_ns)
    client_job = client.capture_request(wait=False, flush=trigger_monotonic_ns)
    server_request = None
    client_request = None
    align_attempts = 0
    half_frame_ns = frame_period_ns // 2

    try:
        server_request = wait_job(
            server, server_job, request_timeout_s, "capturing server frame"
        )
        client_request = wait_job(
            client, client_job, request_timeout_s, "capturing client frame"
        )

        while True:
            server_timestamp = get_sensor_timestamp(server_request, "server")
            client_timestamp = get_sensor_timestamp(client_request, "client")
            delta_ns = client_timestamp - server_timestamp
            if abs(delta_ns) <= half_frame_ns:
                return server_request, client_request, align_attempts, delta_ns
            if align_attempts >= max_align_attempts:
                raise RuntimeError(
                    "Could not match frames within half a frame period: "
                    f"delta_ns={delta_ns}, frame_period_ns={frame_period_ns}"
                )

            align_attempts += 1
            if delta_ns < 0:
                client_request.release()
                client_request = capture_request(
                    client,
                    request_timeout_s,
                    "advancing client to matching frame period",
                )
            else:
                server_request.release()
                server_request = capture_request(
                    server,
                    request_timeout_s,
                    "advancing server to matching frame period",
                )
    except Exception:
        if server_request is not None:
            server_request.release()
        if client_request is not None:
            client_request.release()
        raise


def capture_pair(
    server: Any,
    client: Any,
    args: argparse.Namespace,
    index: int,
    trigger_monotonic_ns: int,
    frame_period_ns: int,
) -> dict[str, Any]:
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    stem = f"{args.prefix}_{index:06d}_{stamp}"
    server_jpeg = args.output_dir / f"{stem}_server.jpg"
    client_jpeg = args.output_dir / f"{stem}_client.jpg"
    metadata_json = args.output_dir / f"{stem}.json"

    requests = capture_matched_requests(
        server,
        client,
        trigger_monotonic_ns,
        args.request_timeout_s,
        frame_period_ns,
        args.max_align_attempts,
    )
    server_request, client_request, align_attempts, sensor_delta_ns = requests
    try:
        server_metadata = dict(server_request.get_metadata())
        client_metadata = dict(client_request.get_metadata())
        server_request.save("main", str(server_jpeg))
        client_request.save("main", str(client_jpeg))

        server_exposure_start = exposure_start_ns(server_metadata)
        client_exposure_start = exposure_start_ns(client_metadata)
        exposure_delta_ns = None
        if server_exposure_start is not None and client_exposure_start is not None:
            exposure_delta_ns = client_exposure_start - server_exposure_start

        record = {
            "capture_index": index,
            "created_utc": stamp,
            "trigger_monotonic_ns": trigger_monotonic_ns,
            "configuration": {
                "server_camera": args.server_camera,
                "client_camera": args.client_camera,
                "width": args.width,
                "height": args.height,
                "format": args.format,
                "framerate": args.framerate,
                "sync_frames": args.sync_frames,
                "manual": args.manual,
                "requested_exposure_us": args.exposure_us,
                "requested_gain": args.gain,
            },
            "files": {"server": str(server_jpeg), "client": str(client_jpeg)},
            "align_attempts": align_attempts,
            "frame_period_ns": frame_period_ns,
            "sensor_timestamp_delta_ns_client_minus_server": sensor_delta_ns,
            "exposure_start_delta_ns_client_minus_server": exposure_delta_ns,
            "server_metadata": jsonable(server_metadata),
            "client_metadata": jsonable(client_metadata),
        }
        metadata_json.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        record["metadata_file"] = str(metadata_json)
        return record
    finally:
        server_request.release()
        client_request.release()


def install_signal_handlers(
    trigger_event: threading.Event, stop_event: threading.Event
) -> None:
    def handle_trigger(signum: int, frame: Any) -> None:
        del signum, frame
        trigger_event.set()

    def handle_stop(signum: int, frame: Any) -> None:
        del signum, frame
        stop_event.set()
        trigger_event.set()

    signal.signal(signal.SIGUSR1, handle_trigger)
    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)


def start_stdin_trigger_thread(
    trigger_event: threading.Event, stop_event: threading.Event
) -> threading.Thread:
    def run() -> None:
        print("Press Enter to capture a synchronized pair. Ctrl+C to exit.", flush=True)
        while not stop_event.is_set():
            if sys.stdin.readline() == "":
                stop_event.set()
                trigger_event.set()
                return
            trigger_event.set()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def preflight(args: argparse.Namespace, Picamera2: Any, controls: Any) -> int:
    camera_info = Picamera2.global_camera_info()
    print("Detected cameras:")
    for info in camera_info:
        print(f"  {info}")
    camera_numbers = {info.get("Num") for info in camera_info}
    missing = sorted({args.server_camera, args.client_camera} - camera_numbers)
    checks = {
        "SyncModeEnum": hasattr(controls.rpi, "SyncModeEnum"),
        "SyncReady": hasattr(controls.rpi, "SyncReady"),
        "SyncTimer": hasattr(controls.rpi, "SyncTimer"),
        "SyncFrames": hasattr(controls.rpi, "SyncFrames"),
        "capture_sync_request": hasattr(Picamera2, "capture_sync_request"),
        "wait": hasattr(Picamera2, "wait"),
    }
    for name, available in checks.items():
        print(f"{name}: {'available' if available else 'missing'}")
    if missing:
        print(f"Missing camera number(s): {missing}", file=sys.stderr)
    return 0 if not missing and all(checks.values()) else 2


def print_sync_request(role: str, request: Any) -> None:
    metadata = request.get_metadata()
    print(
        f"{role}: SyncReady={metadata.get('SyncReady')} "
        f"SyncTimer={metadata.get('SyncTimer')} "
        f"SensorTimestamp={metadata.get('SensorTimestamp')}",
        flush=True,
    )


def main() -> int:
    args = parse_args()
    Picamera2, controls = import_camera_api()
    if args.preflight:
        return preflight(args, Picamera2, controls)

    camera_info = Picamera2.global_camera_info()
    print("Detected cameras:", flush=True)
    for info in camera_info:
        print(f"  {info}", flush=True)
    camera_numbers = {info.get("Num") for info in camera_info}
    missing = sorted({args.server_camera, args.client_camera} - camera_numbers)
    if missing:
        print(f"Missing requested camera number(s): {missing}", file=sys.stderr)
        return 2

    server = Picamera2(args.server_camera)
    client = Picamera2(args.client_camera)
    server.options["quality"] = args.quality
    client.options["quality"] = args.quality
    server.configure(configure_camera(server, args, controls, "server"))
    client.configure(configure_camera(client, args, controls, "client"))

    trigger_event = threading.Event()
    stop_event = threading.Event()
    install_signal_handlers(trigger_event, stop_event)
    if args.trigger == "stdin":
        start_stdin_trigger_thread(trigger_event, stop_event)

    started_client = False
    started_server = False
    sync_requests: tuple[Any, Any] | None = None
    try:
        print("Starting client first, then server...", flush=True)
        client.start()
        started_client = True
        server.start()
        started_server = True

        print("Waiting for SyncReady from both cameras...", flush=True)
        sync_requests = wait_for_sync_pair(server, client, args.sync_timeout_s)
        print_sync_request("server", sync_requests[0])
        print_sync_request("client", sync_requests[1])
        sync_requests[0].release()
        sync_requests[1].release()
        sync_requests = None
        print("Synchronization is ready.", flush=True)

        if args.trigger == "once":
            trigger_event.set()
        elif args.trigger == "sigusr1":
            print(f"Send SIGUSR1 to capture: kill -USR1 {os.getpid()}", flush=True)

        capture_index = 1
        frame_period_ns = int(round(1_000_000_000 / args.framerate))
        while not stop_event.is_set():
            trigger_event.wait()
            trigger_event.clear()
            if stop_event.is_set():
                break

            trigger_ns = time.monotonic_ns()
            record = capture_pair(
                server, client, args, capture_index, trigger_ns, frame_period_ns
            )
            print(
                f"Saved pair {capture_index}: {record['files']['server']} | "
                f"{record['files']['client']} | "
                f"sensor_delta_ns={record['sensor_timestamp_delta_ns_client_minus_server']} | "
                f"exposure_delta_ns={record['exposure_start_delta_ns_client_minus_server']}",
                flush=True,
            )
            capture_index += 1
            if args.trigger == "once":
                break
            if args.max_captures and capture_index > args.max_captures:
                break
    finally:
        if sync_requests is not None:
            sync_requests[0].release()
            sync_requests[1].release()
        print("Stopping cameras...", flush=True)
        if started_server:
            server.stop()
        if started_client:
            client.stop()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
