
# Raspberry Pi Stringer Natural-Image Visual Stimulus Script

This repo now uses the lab's `rpg` framebuffer path again, not pygame.
That matters for the headless behavior Pi, because the screen is controlled
directly from the Pi framebuffer rather than through a desktop/X11 session.

Current entrypoints:

```text
run_stringer_vstim.py
run_stringer_vstim_cam.py
```

What the runtime does:

- asks for mouse ID and optional session notes
- asks for number of images and repeats, using the current defaults when you just press Enter
- confirms the session settings and estimated playback time before starting
- chooses a reproducible subset of Stringer center-crop PNGs
- bakes session-specific `rpg` raw files at startup
- displays each image fullscreen for a fixed duration
- shows a uniformly jittered gray ITI between images, sampled in whole display frames
- prints a terminal progress line with percent and ETA during playback
- optionally bakes a photodiode patch into the frames
- logs display-request timestamps for `stim_on` and `iti_on` while the photodiode remains the ground truth for physical onset
- with the camera wrapper, keeps the monitor on the ITI-style gray frame while the camera is started and confirmed, records a pre-stimulus baseline while stimulus raws are prepared, then holds a black post-stimulus screen until you confirm the camera stop/fetch step
- logs planned sequence, request timestamps, baseline metadata, and session metadata

## Default timing

The current default stimulus timing is:

- image on-screen time: `0.5` seconds
- image-to-image ITI: uniformly sampled from `0.7` to `1.0` seconds, inclusive, in whole frames (`42` to `60` frames at 60 Hz)
- initial gray screen: `3.0` seconds
- final gray screen: `3.0` seconds

## Expected paths on the Pi

The script looks for the PNG folder in these places:

```text
~/vstim_natural/stringer_natimg2800_center_crop_png
~/stringer_natimg2800_center_crop_png
./stringer_natimg2800_center_crop_png
```

Session output goes to:

```text
/mnt/hd/<mouse_id>_<YYYYMMDDThhmmssZ>_vstim_natural/
```

## Dependencies

The runtime expects:

- `rpg` installed from the SjulsonLab rpg repository
- `Pillow`
- `RPi.GPIO` on the Pi if GPIO output is enabled
- `ffmpeg` on box 151 if you want automatic `.h264` to `.mp4` conversion

Example rpg install on the Pi:

```bash
cd ~
git clone https://github.com/SjulsonLab/rpg
cd rpg
sudo pip3 install .
```

Or, if the repo is already present somewhere else, install it from that checkout.

Install the Python packages used by this repo:

```bash
pip3 install Pillow
pip3 install RPi.GPIO
```

## Running it

Run the script from the behavior Pi itself, ideally from a local TTY or a plain
SSH shell on the behavior Pi. Do not use X-forwarded sessions for this.

```bash
cd ~/vstim_natural
python3 run_stringer_vstim.py
```

If the basic framebuffer path looks good, try the smoke test:

```bash
python3 fullscreen_test.py
```

If you want camera recording plus automatic file transfer and conversion, use:

```bash
python3 run_stringer_vstim_cam.py
```

That wrapper now asks for a pre-stimulus camera baseline in minutes, then lets
you type `y` and press Enter to start early after the camera process is alive
and its session-specific `.h264` file is confirmed to be growing. The baseline
clock starts from that output-growth confirmation. After the stimulus sequence
finishes, it leaves the screen black until camera stop is verified by both the
tracked PID and the session-specific acquisition-process check. The PID file is
removed only after that verification, and the screen remains black during file
transfer or while a failed transfer is being resolved.

Keep `run_stringer_vstim.py` around as the plain no-camera runner and as the
baseline path if you want to debug the display flow independently.

## Photodiode patch

The photodiode patch is built into the session raw files. The current default
patch is smaller than the first pass version. To adjust it, set:

```python
ENABLE_PHOTODIODE_PATCH = True
```

The helper functions already bake the patch into the session raw files.

## Randomization and timing records

Image selection, trial order, and ITI jitter use independent random-number
generators. The resolved seeds are written to the session metadata. The
planned-sequence CSV records `planned_stim_duration_sec`,
`planned_iti_frames`, and `planned_iti_duration_sec` for every trial, and the
event log records the same planned ITI for each `iti_on` event.

Only the 19 possible ITI raw files are cached per session:
`gray_iti_042f.raw` through `gray_iti_060f.raw`.

## Notes on the display backend

The older pygame approach was a dead end for the headless behavior Pi.
The rpg path is the one the lab code already uses for framebuffer display,
and it is the right place to put this stimulus runner.
