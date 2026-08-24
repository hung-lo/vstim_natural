
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
- confirms the session settings and exact planned stimulus-sequence duration before starting
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

Both runners also accept non-interactive overrides, for example:

```bash
python3 run_stringer_vstim.py \
  --mouse-id mouse_01 --num-images 5 --repeats 2 \
  --stimulus-duration-sec 0.5 --iti-min-sec 0.7 --iti-max-sec 1.0
```

The camera runner requires an explicit camera choice when started. Use
`--camera` to require recording or `--no-camera` to disable it; without either
flag it asks before any session files or camera processes are touched. The
camera status line reports a separate recording timer, task progress, exact
realized task ETA, and a finish estimate. The recording timer begins only
after camera readiness is confirmed and freezes when stop is confirmed.

That wrapper now asks for a pre-stimulus camera baseline in minutes, then lets
you type `y` and press Enter to start early after the camera process is alive
and its session-specific `.h264` file is confirmed to be growing. The baseline
clock starts from that output-growth confirmation, and the PRE status countdown
updates live after recording is confirmed. After the stimulus sequence
finishes, it leaves the screen black until camera stop is verified by both the
tracked PID and the session-specific acquisition-process check. The PID file is
removed only after that verification, and the screen remains black during file
transfer or while a failed transfer is being resolved.

During the open-ended black post-stimulus baseline, press Enter or type `y`,
`yes`, `s`, or `stop`, then press Enter to stop the camera and begin
verification/fetch. The screen remains black while camera stop, transfer,
raw-file verification, MP4 conversion, and remote raw cleanup are completed.
Type `l` or `leave` and press Enter only when you explicitly need to leave the
camera running; the session is then marked
`stimulus_complete_camera_left_running`.

The planned task ETA uses the exact realized ITIs in the session sequence.
Camera transfer/conversion and the open-ended black baseline are not included
in that planned estimate. The photodiode remains the physical display-timing
ground truth; software timestamps describe display requests and operational
state.

Keep `run_stringer_vstim.py` around as the plain no-camera runner and as the
baseline path if you want to debug the display flow independently.

## Camera recovery and file integrity

The camera controller stores its session state in:

```text
/mnt/hd/.vstim_natural_camera_session.json
```

The state is namespaced to this repository and protocol. An older generic
`.last_remote_camera_session.json` is rejected rather than used for automatic
stop, fetch, or remote deletion, because its session ownership cannot be
proven safely.

Camera fetch is deliberately non-destructive at first: `rsync` copies the raw
`.h264` files without deleting them from box 152. The controller then verifies
remote and local size/SHA-256 manifests, converts and strictly validates the
`.mp4` files with `ffprobe`, and only then deletes the exact verified raw paths
on box 152. If transfer, conversion, validation, or cleanup fails, raw files
are retained so `fetch` or `convert` can be retried safely.

The transfer uses `--partial --append-verify` and has a separate long timeout
for remote file listing and SHA-256 generation. Each session also contains a
local `video/video_manifest.json` archival record. Standalone `convert` first
rechecks the current local H.264 against the saved raw manifest and can finish
the same exact-path remote cleanup after successful MP4 validation.

After remote cleanup succeeds, a repeated `fetch` is local and idempotent: it
re-hashes the local H.264 and revalidates the MP4 without SSH, rsync, or remote
deletion. Preview input reaching EOF is treated as an abnormal condition; the
preview is stopped, but EOF is not interpreted as typing `y`.

`session_completed` means that stimulus playback completed and camera stop was
confirmed. It does not by itself mean that camera data are fully archived.
Use `camera_data_secured`, which additionally requires raw SHA-256 verification,
MP4 validation, and confirmed remote raw cleanup.

Every session has an atomic `session_manifest.json` with relative paths to the
session metadata, event logs, planned sequence, selected-image manifest, and
camera manifests. The metadata records schema versions, monotonic phase and
camera timing, exact realized ITIs, and operational statuses including
`complete`, `protocol_complete_video_pending`,
`protocol_complete_camera_cleanup_failed`,
`stimulus_complete_camera_left_running`, `interrupted`, `failed`, and
`incomplete`.

Manifest artifact entries are existence-aware: an artifact is listed by its
relative path only after it exists, and unavailable optional camera artifacts
are recorded as `null` rather than as fabricated paths.

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
`planned_iti_frames`, `planned_iti_duration_sec`, and `iti_will_play` for every
trial. Session metadata records `planned_sequence_duration_sec`, and the event
log records the same planned ITI for each `iti_on` event.

The two runners intentionally retain different final-ITI policies. The plain
`run_stringer_vstim.py` runner plays the final planned ITI before the final
gray screen and records `final_iti_policy=played_before_final_gray`. The
camera runner skips the final planned ITI and transitions directly to its
controlled post-stimulus gray/black workflow, recording
`final_iti_policy=skipped_before_poststim_gray`. These metadata fields describe
the existing behavior; they do not change timing.

Only the 19 possible ITI raw files are cached per session:
`gray_iti_042f.raw` through `gray_iti_060f.raw`.

## Notes on the display backend

The older pygame approach was a dead end for the headless behavior Pi.
The rpg path is the one the lab code already uses for framebuffer display,
and it is the right place to put this stimulus runner.
