# IMX283 Camera Bring-Up and Dual-Camera Test Notes

## Purpose

This document records the IMX283 camera bring-up work performed on a Raspberry Pi Compute Module 4 IO Board and a Raspberry Pi 5 Model B. It includes the working boot configuration, test commands, observed failures, troubleshooting steps, and the final dual-camera result.

The Raspberry Pi 5 bring-up proved that both IMX283 modules can be detected, streamed independently, and captured concurrently. Subsequent software-sync tests measured frame timestamp alignment and are documented in [software-sync-validation.md](software-sync-validation.md).

## Tested Hardware and Software

### Compute Module 4 setup

- Raspberry Pi Compute Module 4 with the official CM4 IO Board
- One third-party Sony IMX283 camera module
- Camera connected through the CM4 IO Board CSI connectors
- Test host: `rpi@192.168.100.210`

### Raspberry Pi 5 setup

- Raspberry Pi 5 Model B Rev 1.0
- Two matching third-party Sony IMX283 camera modules
- One camera on each 22-pin CAM/DISP connector, referred to here as CSI0 and CSI1
- Test host: `rpi@192.168.0.192`
- Debian GNU/Linux 13 (trixie), Debian version 13.5
- Kernel: `6.18.34+rpt-rpi-2712`
- `libcamera` reported by the test tools: `0.7.1+rpt20260609`
- `libpisp`: `v1.5.0`
- Camera applications: `rpicam-hello` and `rpicam-still`

SSH passwords and other credentials are intentionally not recorded in this document.

## IMX283 Interface Requirements

The tested IMX283 overlay uses a four-lane MIPI CSI-2 connection. This is important when selecting a Raspberry Pi connector, FPC cable, or 15-pin-to-22-pin adapter.

On the CM4 IO Board, CAM1 exposes the four-lane camera interface needed by this module. CAM0 is only a two-lane interface and did not work with this IMX283 module. The single-camera CM4 test therefore used CAM1.

Raspberry Pi 5 provides two four-lane MIPI transceivers through its two CAM/DISP connectors, so it can electrically support two four-lane IMX283 modules, subject to correct cables, module pinout, power, driver support, and system bandwidth.

## Boot Configuration

Automatic camera detection was disabled and the IMX283 overlays were loaded explicitly in `/boot/firmware/config.txt`.

### Single camera on the CM4 IO Board CAM1 connector

```ini
camera_auto_detect=0
dtoverlay=imx283
```

This configuration successfully detected the sensor and captured a JPEG on CAM1.

### Two cameras on Raspberry Pi 5

```ini
camera_auto_detect=0

# Other platform overlays remain unchanged.

[all]
# Dual IMX283 cameras for Pi 5 software sync validation
dtoverlay=imx283,cam0
dtoverlay=imx283
```

The `cam0` parameter selects the first Raspberry Pi 5 camera path. The overlay without `cam0` selects the other path.

During troubleshooting, the original Raspberry Pi 5 configuration was backed up as:

```text
/boot/firmware/config.txt.bak-dual-imx283-20260716-192347
```

The original dual-camera configuration was restored after the isolated tests and was also used for the final successful test.

## Useful Overlay Parameters

The installed `imx283` overlay reported these relevant parameters:

- `cam0`: select the alternate Raspberry Pi 5 camera interface
- `clock-frequency`: sensor clock, with 24 MHz as the default and 12 MHz as an alternative
- `link-frequency`: CSI-2 link frequency, with 720 MHz as the default and 360 MHz as an alternative
- `rotation` and `orientation`: sensor mounting metadata
- `media-controller`: media-controller behavior

The final working setup used the default clock and link frequency. The lower clock and link settings were only diagnostic experiments.

## Basic Detection and Capture Commands

List all detected cameras:

```bash
rpicam-hello --list-cameras
```

Run a short, headless stream test:

```bash
rpicam-hello --nopreview -t 2000
```

Capture from a specific camera:

```bash
rpicam-still --camera 0 --nopreview -t 1500 -o cam0.jpg
rpicam-still --camera 1 --nopreview -t 1500 -o cam1.jpg
```

Capture using the tested 1824 x 1216 mode:

```bash
rpicam-still --camera 0 --nopreview --width 1824 --height 1216 -t 1500 -o cam0.jpg
rpicam-still --camera 1 --nopreview --width 1824 --height 1216 -t 1500 -o cam1.jpg
```

Inspect camera-related kernel messages:

```bash
dmesg | grep -Ei 'imx283|rp1-cfe|chip id|Error reading|timed out'
```

## CM4 IO Board Test Result

With the camera connected to CAM1 and `dtoverlay=imx283` enabled, the camera was detected as:

```text
imx283 [5472x3648 12-bit RGGB]
```

A successful test image was captured to:

```text
/home/rpi/imx283-test.jpg
```

The image was also copied to the development Mac at:

```text
/Users/andyliu/Documents/Codex/2026-07-03/sudo-reboot/outputs/imx283-test.jpg
```

Moving the same setup to CAM0 failed because that CM4 interface does not provide the four CSI-2 data lanes required by this module.

## Initial Raspberry Pi 5 Dual-Camera Failure

The Raspberry Pi 5 initially booted with both overlays enabled, but only one camera appeared in `rpicam-hello --list-cameras`.

### CSI0 / `cam0` path

The sensor on the `cam0` path failed during the I2C chip-ID read:

```text
imx283 10-001a: Error reading reg 0x3000: -121
imx283 10-001a: failed to read chip id b, with error -121
imx283 10-001a: probe with driver imx283 failed with error -121
```

Linux error `-121` is `EREMOTEIO`, which means the sensor did not respond correctly to the I2C transaction. At this stage, likely causes included an incompletely seated or reversed cable, incompatible adapter pinout, missing module power/reset, or a faulty module or cable.

### CSI1 / default path

The sensor on the default path responded over I2C and was listed correctly, but image streaming failed with:

```text
Camera frontend has timed out!
Please check that your camera sensor connector is attached securely.
ERROR: Device timeout detected, attempting a restart!!!
```

This showed that control communication was working, while valid MIPI image data was not reaching the capture frontend.

## Isolated Diagnostic Tests

Each camera path was tested independently by temporarily loading only one overlay and rebooting.

### CSI0-only tests

The following configurations were tested:

```ini
dtoverlay=imx283,cam0
```

and:

```ini
dtoverlay=imx283,cam0,clock-frequency=12000000,link-frequency=360000000
```

Both produced the same I2C chip-ID error `-121`. Lowering the sensor clock and CSI link frequency did not correct the problem.

### CSI1-only tests

The following configurations were tested:

```ini
dtoverlay=imx283
```

```ini
dtoverlay=imx283,link-frequency=360000000
```

```ini
dtoverlay=imx283,clock-frequency=12000000,link-frequency=360000000
```

The camera was detected in all three cases, but every stream or still-capture attempt timed out. The reduced link setting took effect, as confirmed by the lower reported mode frame rates and kernel link rate, but did not solve the missing image stream.

These results made a simple overlay clock or link-rate mismatch unlikely.

## Cable and Camera Swap Test

Both camera modules were powered off and swapped between CSI0 and CSI1 together with their respective cables. After the swap and reseating, both sensors were detected on the next boot:

```text
0 : imx283 [5472x3648 12-bit RGGB]
    /base/axi/pcie@1000120000/rp1/i2c@88000/imx283@1a

1 : imx283 [5472x3648 12-bit RGGB]
    /base/axi/pcie@1000120000/rp1/i2c@80000/imx283@1a
```

The corresponding capture paths were:

| Camera index | I2C path | CSI capture block |
| --- | --- | --- |
| `0` | `i2c@88000`, sensor `10-001a` | `1f00110000.csi` |
| `1` | `i2c@80000`, sensor `11-001a` | `1f00128000.csi` |

Both capture frontends registered successfully, and neither sensor produced the earlier chip-ID error.

Because both cameras worked after the swap, this test did not isolate one permanently faulty module or cable. It strongly indicates that at least one original FPC connection was not fully seated, had poor contact, or was corrected during the swap. Cable orientation and connector engagement should be checked carefully whenever the problem reappears.

## Successful Independent Capture

Each camera was selected explicitly and captured independently at 1824 x 1216.

Results:

| Camera | Exit status | File size | Result |
| --- | ---: | ---: | --- |
| Camera 0 | `0` | 40,932 bytes | Valid 1824 x 1216 JPEG |
| Camera 1 | `0` | 44,727 bytes | Valid 1824 x 1216 JPEG |

Both applications reported:

```text
Still capture image received
```

The files on the Raspberry Pi were:

```text
/home/rpi/imx283-cam0.jpg
/home/rpi/imx283-cam1.jpg
```

Copies were saved on the development Mac under:

```text
/Users/andyliu/Documents/Codex/2026-07-03/sudo-reboot/outputs/pi5-dual-imx283-swapped/
```

The images were structurally valid and contained image data, although both were very dark with only small illuminated regions. This likely reflects the scene, lens cap, lens mounting, aperture, exposure, or available light rather than a CSI transport failure.

## Successful Concurrent Capture

Two `rpicam-still` processes were launched concurrently, one for each camera. The essential command pattern was:

```bash
rpicam-still --camera 0 --nopreview --width 1824 --height 1216 \
  -t 2000 -o /home/rpi/imx283-dual-cam0.jpg &

rpicam-still --camera 1 --nopreview --width 1824 --height 1216 \
  -t 2000 -o /home/rpi/imx283-dual-cam1.jpg &

wait
```

Both processes exited with status `0`, both reported `Still capture image received`, and both files were created:

| Camera | Exit status | File size |
| --- | ---: | ---: |
| Camera 0 | `0` | 39,552 bytes |
| Camera 1 | `0` | 44,760 bytes |

This verifies concurrent dual-camera operation on the Raspberry Pi 5 at the tested resolution. Starting two userspace processes at approximately the same time is not proof of sensor-level synchronization. Their exposure start times and frame timestamps must be measured before describing the system as synchronized.

## Troubleshooting Guide

### No camera listed and I2C error `-121`

1. Shut down and remove power before touching an FPC cable.
2. Reseat both cable ends and close the connector latches fully.
3. Confirm the exposed cable contacts face the correct direction at both ends.
4. Confirm the cable or adapter carries all four MIPI data lanes and uses the correct pinout.
5. Swap the complete camera-and-cable assembly between ports.
6. Swap only the cables to distinguish a cable problem from a module problem.
7. Check module power, reset, power-down, and reference-clock requirements.

If the error follows a camera-and-cable assembly, investigate that module or cable. If it stays on one Raspberry Pi connector, investigate that connector, adapter, and cable orientation.

### Camera listed but capture frontend times out

This normally means sensor control over I2C is working but image packets are not being received correctly. Check:

- Four-lane MIPI continuity and adapter compatibility
- Cable seating and orientation
- Damaged or excessively long FPC cables
- Module-specific lane mapping
- Sensor clock, power, reset, and standby behavior
- Kernel messages from `rp1-cfe`

Changing `clock-frequency` and `link-frequency` can be useful for diagnosis, but it did not solve the initial wiring/contact problem in this test.

### Both cameras work but images are dark

Check the lens cap, lens focus and seating, aperture, scene lighting, and exposure controls. A valid but dark JPEG still confirms that sensor frames crossed the CSI link and reached the image-processing pipeline.

## Current Verified State

As of 2026-07-16:

- The CM4 IO Board works with one IMX283 on its four-lane CAM1 interface.
- The Raspberry Pi 5 detects two IMX283 modules with the dual-overlay configuration.
- Camera 0 captures successfully.
- Camera 1 captures successfully.
- Both cameras capture concurrently at 1824 x 1216.
- The final working setup uses default IMX283 clock and CSI link frequencies.
- The active PiSP IMX283 tuning file requires an `rpi.sync` algorithm entry on the tested image.
- Both cameras reach `SyncReady=True` after that tuning entry is enabled.
- Triggered JPEG pairs were validated at 2736 x 1824 and 5472 x 3648.
- Measured sensor timestamp alignment was in the tens of microseconds during the completed tests.

## Remaining Tests for Production Use

1. Run a long triggered-capture soak test and measure worst-case and percentile timestamp error.
2. Test repeated full-resolution capture for dropped frames, thermal behavior, and bandwidth limits.
3. Validate alignment with a moving visual target or timestamped light source.
4. Verify whether the camera modules expose a common trigger, XVS, or synchronization signal.
5. If hardware triggering is available, wire both modules to a shared trigger and confirm the driver exposes the required controls.
6. Add a GPIO input adapter if the production trigger is a physical signal.
