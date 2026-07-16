# Raspberry Pi 5 Dual IMX283 Software Sync

This project captures JPEG pairs from two IMX283 cameras on one Raspberry Pi 5. It uses the Raspberry Pi `libcamera` software synchronization algorithm, keeps both cameras streaming, and saves a matched pair when the process receives a trigger.

The tested setup reached `SyncReady=True` and produced frame timestamp offsets in the tens of microseconds. See [docs/software-sync-validation.md](docs/software-sync-validation.md) for the measurements and limitations.

## Project Layout

- `scripts/dual_imx283_sync_capture.py`: long-running synchronized JPEG capture service
- `tools/imx283_sync_probe.py`: frame-by-frame synchronization measurement
- `tools/exposure_sweep_capture.py`: synchronized manual-exposure sweep for visual timing targets
- `tools/imx283_tuning_sync.py`: check, enable, and restore the PiSP `rpi.sync` tuning entry
- `tools/system_check.py`: read-only setup and camera API checks
- `tools/summarize_captures.py`: summarize timestamp deltas from output JSON files
- `docs/imx283-camera-testing.md`: CM4 and Raspberry Pi 5 camera bring-up history
- `docs/software-sync-validation.md`: root cause, test procedure, results, and remaining work
- `docs/display-clock-validation.md`: 120 Hz display-clock exposure sweep and interpretation

## Tested Platform

- Raspberry Pi 5 Model B
- Two matching Sony IMX283 camera modules
- Debian 13 (trixie)
- Kernel `6.18.34+rpt-rpi-2712`
- `rpicam-apps` v1.12.0
- `libcamera` `0.7.1+rpt20260609`
- Picamera2 with `capture_sync_request()` and `wait()` support

Package versions change over time. Run the checks below on the target Pi before relying on this setup.

## Hardware and Boot Setup

Power the Pi off before connecting or reseating either camera. Each tested IMX283 requires four CSI-2 data lanes. The two Raspberry Pi 5 CAM/DISP connectors can each provide four lanes with the correct 22-pin cable and camera-module pinout.

Disable automatic camera detection and load one overlay for each connector in `/boot/firmware/config.txt`:

```ini
camera_auto_detect=0

[all]
dtoverlay=imx283,cam0
dtoverlay=imx283
```

Reboot, then verify that both sensors are listed:

```bash
sudo reboot
rpicam-hello --list-cameras
```

The tested camera mapping was:

| Role | Camera number | Sensor path |
| --- | ---: | --- |
| Server | `0` | `/base/axi/pcie@1000120000/rp1/i2c@88000/imx283@1a` |
| Client | `1` | `/base/axi/pcie@1000120000/rp1/i2c@80000/imx283@1a` |

Always confirm camera numbering after changing overlays, cables, or OS images.

## Software Setup

Install the Raspberry Pi camera applications and Python bindings if they are not already present:

```bash
sudo apt update
sudo apt install rpicam-apps python3-picamera2 python3-yaml
```

Run the read-only checks from the project root:

```bash
python3 tools/system_check.py --strict
python3 scripts/dual_imx283_sync_capture.py --preflight
```

### Enable the IMX283 sync algorithm

On the tested image, the Pi 5 tuning file did not contain the `rpi.sync` algorithm. The sync controls were visible, but no synchronization took place until this entry was added.

Check the tuning file:

```bash
python3 tools/imx283_tuning_sync.py --check
```

If it reports `rpi.sync=missing`, apply the change as root:

```bash
sudo python3 tools/imx283_tuning_sync.py --apply
```

The utility parses libcamera's relaxed JSON syntax, creates a timestamped backup next to the original file, writes the update atomically, and prints the backup path. `python3-yaml` is required because some tuning files contain trailing commas that libcamera accepts but strict JSON rejects. Stop and restart camera applications after applying it. A reboot is not normally required.

Package upgrades may replace `/usr/share/libcamera/ipa/rpi/pisp/imx283.json`. Re-run `system_check.py` after camera-stack upgrades.

To restore a backup explicitly:

```bash
sudo python3 tools/imx283_tuning_sync.py \
  --restore /usr/share/libcamera/ipa/rpi/pisp/imx283.json.before-rpi-sync-TIMESTAMP
```

## Validate Synchronization

Run a 130-frame probe at the recommended starting mode:

```bash
python3 tools/imx283_sync_probe.py \
  --width 2736 --height 1824 \
  --framerate 5 \
  --frames 130 \
  --sync-frames 100 \
  --save-last \
  --output-dir sync-probe
```

Expected signs of success are:

- libcamera logs contain `Sync mode set to client`, `Sync mode set to server`, and `Sync achieved`
- both metadata streams eventually report `SyncReady=True`
- post-sync `delta_us` values are in the expected tens-of-microseconds range

At 5 fps, `--sync-frames 100` needs about 20 seconds before `SyncReady`. At 2 fps, it needs about 50 seconds, so the main script uses a 90-second synchronization timeout by default.

## Capture JPEG Pairs

For repeatable stereo pairs, fixed exposure and gain are recommended:

```bash
python3 scripts/dual_imx283_sync_capture.py \
  --server-camera 0 \
  --client-camera 1 \
  --width 2736 --height 1824 \
  --framerate 5 \
  --manual --exposure-us 10000 --gain 1.0 \
  --trigger sigusr1 \
  --output-dir captures
```

Wait until the process prints `Synchronization is ready.` It will also print its PID and the exact trigger command:

```bash
kill -USR1 PID
```

Each signal saves:

- one `_server.jpg`
- one `_client.jpg`
- one `.json` metadata record

Signals should be sent one at a time after the previous pair is reported as saved. Closely spaced signals can be coalesced because `SIGUSR1` is used as an edge notification, not a queued message transport.

Other trigger modes are useful for testing:

```bash
# Capture once as soon as synchronization is ready.
python3 scripts/dual_imx283_sync_capture.py --trigger once

# Capture each time Enter is pressed.
python3 scripts/dual_imx283_sync_capture.py --trigger stdin
```

The tested full-resolution one-shot command was:

```bash
python3 scripts/dual_imx283_sync_capture.py \
  --width 5472 --height 3648 \
  --framerate 2 \
  --buffer-count 4 \
  --sync-frames 100 \
  --sync-timeout-s 90 \
  --manual --exposure-us 10000 --gain 1.0 \
  --trigger once \
  --output-dir captures-fullres
```

## Interpret the Metadata

The key JSON fields are:

- `sensor_timestamp_delta_ns_client_minus_server`: client sensor timestamp minus server timestamp
- `exposure_start_delta_ns_client_minus_server`: timestamp delta corrected by each frame's reported exposure time
- `align_attempts`: extra requests needed to choose frames from the same frame period
- `server_metadata.SyncReady` and `client_metadata.SyncReady`: libcamera synchronization status
- `server_metadata.SyncTimer` and `client_metadata.SyncTimer`: synchronization algorithm timer state

Summarize one capture directory or a directory tree:

```bash
python3 tools/summarize_captures.py captures
```

## Display Clock Exposure Sweep

Use the sweep tool to find the shortest readable exposure for an external timing display while both cameras remain synchronized:

```bash
python3 tools/exposure_sweep_capture.py \
  --width 2736 --height 1824 \
  --framerate 5 \
  --gain 16 \
  --exposures-us 100,200,400,800,1200,1600,2400,3200,4800,6400,8000 \
  --settle-frames 2 \
  --output-dir clock-exposure-sweep
```

The tool flushes buffered frames after every control change and waits until both metadata streams confirm the requested exposure before saving a pair. On the tested scene, the sensor quantized requested exposures of 100, 200, and 400 us to 93, 194, and 396 us. A requested 200 us at analogue gain 16 was the shortest practical setting; 100 us was visible but dim and susceptible to display-refresh artifacts.

A 120 Hz display changes state only every 8.33 ms. It can provide a coarse frame-state check, but it cannot measure tens-of-microseconds camera alignment. Rolling-shutter row timing, display scanout, camera orientation, and the target's row position can make two synchronized cameras show adjacent display states. See [docs/display-clock-validation.md](docs/display-clock-validation.md) before interpreting visible millisecond differences.

## Operational Notes

- Software sync removes long-term drift and reached tens-of-microseconds alignment in this setup. It is not a hardware trigger and does not prove zero-time exposure alignment.
- Keep both cameras running continuously. Reconfiguring or restarting a camera requires synchronization to settle again.
- Manual exposure avoids independently changing exposure durations and image brightness between the two cameras.
- The current trigger is a Linux process signal. A physical GPIO trigger adapter is not included yet.
- Keep each metadata JSON beside its JPEG pair; image filenames alone do not prove synchronization.
- Run a long soak test at the final resolution, frame rate, exposure, lighting, and trigger interval before production use.

## Troubleshooting

If both cameras are listed but `SyncReady` never appears, check the active PiSP tuning file first:

```bash
python3 tools/imx283_tuning_sync.py --check
```

If one camera disappears or reports I2C error `-121`, power down and reseat both ends of its FPC cable. If a sensor is listed but the capture frontend times out, check cable orientation, four-lane continuity, module pinout, and connector engagement. The detailed bring-up history is in [docs/imx283-camera-testing.md](docs/imx283-camera-testing.md).
