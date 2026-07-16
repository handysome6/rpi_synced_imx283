# Dual IMX283 Software Synchronization Validation

## Scope

This document records the software synchronization work completed on 2026-07-16 with two IMX283 cameras on one Raspberry Pi 5. The target was triggered JPEG-pair capture. Synchronized video recording and external hardware triggering were outside the test scope.

## System Under Test

- Raspberry Pi 5 Model B Rev 1.0
- Two matching Sony IMX283 camera modules
- Debian 13 (trixie)
- Kernel `6.18.34+rpt-rpi-2712`
- `rpicam-apps` v1.12.0
- `libcamera` `0.7.1+rpt20260609`
- PiSP camera pipeline

The active boot configuration contained:

```ini
camera_auto_detect=0
dtoverlay=imx283,cam0
dtoverlay=imx283
```

Both cameras exposed these modes:

| Resolution | Bit depth | Reported maximum frame rate |
| ---: | ---: | ---: |
| 5472 x 3648 | 10-bit | 18.31 fps |
| 1824 x 1216 | 12-bit | 49.60 fps |
| 2736 x 1824 | 12-bit | 36.17 fps |
| 5472 x 3648 | 12-bit | 18.31 fps |

## Initial Failure

Picamera2 and libcamera exposed `SyncMode`, `SyncFrames`, `SyncReady`, and `SyncTimer`, and accepted server/client controls. However, metadata did not contain `SyncReady` or `SyncTimer`, and the two sensor timestamps stayed approximately 48 to 52 milliseconds apart.

The problem was not the Python call sequence. The Pi 5 used this tuning file:

```text
/usr/share/libcamera/ipa/rpi/pisp/imx283.json
```

Its `algorithms` array did not contain `rpi.sync`. The VC4 IMX283 tuning file and other PiSP sensor tuning files contained that algorithm, which provided the comparison that identified the omission.

## Required Tuning Change

The following object was added to the PiSP IMX283 `algorithms` array:

```json
{
    "rpi.sync": {}
}
```

The original tuning file was backed up before modification. The repository utility `tools/imx283_tuning_sync.py` makes this operation repeatable, validates libcamera's relaxed JSON syntax, writes atomically, and supports explicit restoration. The relaxed parser is necessary because the tested tuning file contains trailing commas accepted by libcamera but rejected by Python's strict JSON parser.

After the change, libcamera logged messages equivalent to:

```text
Sync mode set to client
Sync mode set to server
*** Sync achieved! Difference 35us
```

Metadata then reported `SyncReady=True` for both cameras.

## Tested Capture Sequence

The client camera was started first and the server second. Both cameras stayed in one video-style configuration. The server used `SyncFrames=100`; the script did not accept triggers until both streams reached `SyncReady`.

For each trigger, requests for both cameras were submitted with the same monotonic flush timestamp. Their `SensorTimestamp` values were compared, and an older request was advanced when the pair came from different frame periods. The selected requests were saved as JPEGs with a JSON metadata record.

The current repository implementation uses asynchronous Picamera2 jobs followed by `Picamera2.wait(job, timeout=...)`. This matters because the `wait` argument on `capture_request()` and `capture_sync_request()` is a synchronous/asynchronous selector in the tested Picamera2 version, not a timeout in seconds.

## Results

| Test | Resolution | Rate | Result |
| --- | ---: | ---: | --- |
| Synchronization probe | 1824 x 1216 | 5 fps | First ready frame index 101; 29 ready samples ranged from -72 to -16 us; mean absolute delta 48.5 us |
| Synchronization probe | 2736 x 1824 | 5 fps | First ready frame index 101; 29 ready samples ranged from -59 to +16 us; mean absolute delta 36.9 us |
| Full-resolution one-shot | 5472 x 3648 | 2 fps | Valid JPEG pair; sensor and exposure-start delta -36 us |
| Repeated signal trigger | 2736 x 1824 | 5 fps | Three valid JPEG pairs; deltas -33, -40, and -45 us; mean absolute delta 39.3 us |

All saved JPEGs had the expected dimensions. The repeated trigger test used three separate `SIGUSR1` signals after synchronization was ready.

## Repository Implementation Revalidation

After the scripts were organized in this repository, the new implementations were run again on the same Raspberry Pi 5:

- `tools/system_check.py --strict` passed the boot configuration, tuning, API, and two-camera checks.
- A 110-frame `2736 x 1824` probe reached `SyncReady=True` at frame index 101. Its nine ready samples ranged from -40 to -30 us, with a mean absolute delta of 35.44 us.
- `scripts/dual_imx283_sync_capture.py --trigger once` saved two valid `2736 x 1824` JPEGs and one metadata JSON file. The sensor timestamp and exposure-start deltas were both -36 us.
- The repository version's explicit Picamera2 job timeout path completed successfully.

## Conclusion

The tested Raspberry Pi 5 can operate two four-lane IMX283 cameras and capture software-synchronized JPEG pairs after enabling `rpi.sync` in the active PiSP IMX283 tuning file. The measured alignment was consistently in the tens of microseconds during these tests, with no observed frame-period mismatch in the saved trigger pairs.

This validates the proposed software-sync path for triggered still-image capture. It does not establish hardware-level simultaneity, bound worst-case latency over long runs, or validate a physical external trigger input.

## Remaining Validation

Before production use:

1. Run 100 to 1,000 triggered pairs at the final resolution, rate, exposure, and trigger interval.
2. Report maximum and percentile absolute timestamp error, not only the mean.
3. Check for missing JPEGs, partial pairs, request timeouts, thermal throttling, and kernel camera errors.
4. Validate synchronization with a moving visual target or timestamped light source.
5. Re-run the tuning check after libcamera or Raspberry Pi camera-package upgrades.
6. Add and debounce a GPIO trigger adapter if the required signal is physical rather than a Linux process signal.
