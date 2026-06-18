# Raspberry Pi Stringer Natural-Image Visual Stimulus Script

This folder contains a runnable Raspberry Pi stimulus script for presenting a subset of the Stringer/Pachitariu natural-image library during 2P imaging.

The script is designed for a passive visual stimulus experiment with:

- one 1080p display,
- natural images shown in randomized order,
- repeated image presentations for decoding,
- gray inter-trial interval (ITI),
- top-right photodiode patch,
- TTL pulse at image onset,
- UTC Unix timestamp logging, assuming the Raspberry Pi clock is synchronized, for example with NeuroKairos.

## Files

```text
run_stringer_vstim.py   Main runnable stimulus script
README.md               This documentation
```

## Quick start

1. Copy this folder to the Raspberry Pi.
2. Make sure your image PNGs are in the folder configured by `IMAGE_DIR` in `run_stringer_vstim.py`.
3. Install dependencies if needed:

```bash
pip install pygame
```

On a Raspberry Pi with GPIO output enabled, `RPi.GPIO` is also needed. It is often preinstalled on Raspberry Pi OS, but can usually be installed with:

```bash
pip install RPi.GPIO
```

4. Edit the `USER SETTINGS` section at the top of `run_stringer_vstim.py`.
5. Run:

```bash
python3 run_stringer_vstim.py
```

6. Enter the mouse ID when prompted.

## Running over SSH

If you connect with `ssh -X` or `ssh -Y`, the remote shell usually exports a
forwarded X11 display such as `DISPLAY=localhost:10.0`. Without an override,
pygame/SDL will open on the forwarded display instead of the behavior Pi HDMI
screen.

`run_stringer_vstim.py` now detects that case and, by default, switches to the
behavior Pi's local desktop display:

- `DISPLAY=:0`
- `SDL_VIDEODRIVER=x11`
- `XAUTHORITY=~/.Xauthority` when that file exists

At startup, the script prints the original and effective display environment,
as well as the pygame display driver and detected screen size. The same display
routing info is also saved into `metadata.json` for each session.

For the cleanest launch over SSH, avoid X11 forwarding when you do not need it:

```bash
ssh -x pi@behavior-pi
python3 run_stringer_vstim.py
```

If the behavior Pi monitor is showing the Raspberry Pi desktop, the default
`DISPLAY_TARGET = ":0"` and `SDL_VIDEODRIVER_TARGET = "x11"` settings should
be the right choice.

If the behavior Pi monitor is only showing a Linux console/terminal and no
local desktop session is running, X11 `:0` may not exist. In that case, edit
these settings near the top of `run_stringer_vstim.py`:

```python
DISPLAY_TARGET = None
SDL_VIDEODRIVER_TARGET = "kmsdrm"
```

That tells SDL to render directly to the Pi display stack instead of an X11
session.

## Recommended pilot settings

The script currently defaults to a short pilot:

```python
N_IMAGES_TO_USE = 100
N_REPEATS = 5
STIM_DURATION = 0.5
ITI_MODE = "fixed"
GRAY_DURATION = 0.75
TTL_PIN = 23
```

This gives:

```text
100 unique images x 5 repeats = 500 trials
0.5 s stimulus + 0.75 s gray ITI = 1.25 s/trial
Approx stimulus block duration = 10.4 min, plus initial/final gray
```

This is meant to test timing, photodiode, TTL, logging, visual responsiveness, and motion stability.

## Recommended main-experiment settings

After the pilot works, a reasonable main experiment is:

```python
N_IMAGES_TO_USE = 200
N_REPEATS = 8  # or 10
STIM_DURATION = 0.5
ITI_MODE = "jittered"
GRAY_DURATION_MIN = 0.5
GRAY_DURATION_MAX = 1.0
TTL_PIN = 23
```

For 200 images x 10 repeats:

```text
2,000 trials
Mean trial duration = 0.5 s image + 0.75 s gray = 1.25 s
Approx stimulus block duration = 41.7 min, plus initial/final gray
```

## Output files

Each run creates a new session folder:

```text
OUTPUT_ROOT / mouse_id / session_id /
```

For example:

```text
/home/pi/stim_logs/M123/M123_20260617T153000Z/
```

Inside the session folder:

### 1. `selected_images.csv`

The exact image subset used for the session.

Columns:

| Column | Meaning |
|---|---|
| `image_id` | Zero-based image ID within the selected subset, not necessarily the original Stringer ID. |
| `image_filename` | PNG filename. |
| `image_path` | Full path to the image file on the Pi. |

### 2. `planned_sequence.csv`

The randomized trial order generated before stimulus presentation starts.

Columns:

| Column | Meaning |
|---|---|
| `trial_index` | Zero-based trial number. |
| `image_id` | Image ID within the selected subset. |
| `image_filename` | Image shown on that trial. |
| `repeat_number` | Which repeat of that image this trial belongs to. |
| `planned_stim_duration_sec` | Planned image-on duration. |
| `planned_gray_duration_sec` | Planned gray ITI duration. If ITI is jittered, this can differ trial-to-trial. |

### 3. `event_log.csv`

Actual event log written during the experiment.

Columns:

| Column | Meaning |
|---|---|
| `session_id` | Unique session label: mouse ID plus UTC timestamp. |
| `mouse_id` | Mouse ID entered at runtime. |
| `event_type` | Event type, e.g. `stim_on`, `stim_off_gray_on`, `program_exit`. |
| `trial_index` | Trial number, when applicable. |
| `image_id` | Image ID, when applicable. |
| `image_filename` | Filename, when applicable. |
| `repeat_number` | Repeat number, when applicable. |
| `unix_time_utc_sec` | Unix time from `time.time()`. This is UTC if the Pi clock is UTC-synchronized. |
| `perf_counter_sec` | Local monotonic clock from `time.perf_counter()`. Useful for measuring elapsed time within the script. |
| `iso_time_utc` | Human-readable UTC timestamp. |
| `ttl_sent` | `1` if a TTL pulse was sent for this event, otherwise `0`. |
| `photodiode_state` | Expected state of the photodiode patch: bright during image, dark during gray. |
| `planned_stim_duration_sec` | Planned image duration for this trial. |
| `planned_gray_duration_sec` | Planned gray duration for this trial. |
| `notes` | Extra notes for session-level events. |

### 4. `metadata.json`

Session configuration and hardware notes. This is the main record of settings used for the run.

## Variable reference

All key settings are near the top of `run_stringer_vstim.py` under `USER SETTINGS`.

### File paths

#### `IMAGE_DIR`

Folder containing natural-image PNGs.

Example:

```python
SCRIPT_DIR = Path(__file__).resolve().parent
IMAGE_DIR = SCRIPT_DIR / "stringer_natimg2800_center_crop_png"
```

If the repo lives at `/home/pi/vstim_natural`, this resolves to `/home/pi/vstim_natural/stringer_natimg2800_center_crop_png`. The script searches for `*.png` files in this folder. It does not recursively search subfolders.

#### `OUTPUT_ROOT`

Root folder for stimulus logs.

Example:

```python
OUTPUT_ROOT = Path("/home/pi/stim_logs")
```

The script will create a subfolder for each mouse and session.

## Image subset variables

#### `N_IMAGES_TO_USE`

Number of unique images selected from `IMAGE_DIR`.

```python
N_IMAGES_TO_USE = 100
```

Use a smaller number for pilot experiments. Suggested values:

| Use case | Suggested value |
|---|---:|
| Hardware pilot | 20-50 |
| 2P pilot | 100 |
| Main decoding experiment | 200-300 |

Set to `None` only if you want to use all PNGs in `IMAGE_DIR`.

#### `IMAGE_SUBSET_SEED`

Random seed used to select the image subset.

```python
IMAGE_SUBSET_SEED = 777
```

Keep this fixed if you want the same image identities across mice or sessions. Change it only if you intentionally want a different subset.

## Repeat and order variables

#### `N_REPEATS`

Number of times each selected image is shown.

```python
N_REPEATS = 5
```

Total trial count is:

```text
N_IMAGES_TO_USE x N_REPEATS
```

Examples:

| Images | Repeats | Trials |
|---:|---:|---:|
| 100 | 5 | 500 |
| 100 | 10 | 1,000 |
| 200 | 8 | 1,600 |
| 200 | 10 | 2,000 |
| 300 | 6 | 1,800 |

#### `TRIAL_ORDER_SEED`

Random seed used for trial order.

```python
TRIAL_ORDER_SEED = None
```

- `None`: generate a fresh random order each session.
- integer: reproduce the exact same trial order.

The actual seed used is saved in `metadata.json`.

#### `AVOID_ADJACENT_REPEATS`

Whether to try to avoid showing the same image on consecutive trials.

```python
AVOID_ADJACENT_REPEATS = True
```

This does not prevent images from repeating nearby in the session; it only tries to prevent immediate back-to-back repeats.

## Timing variables

#### `STIM_DURATION`

Image-on duration in seconds.

```python
STIM_DURATION = 0.5
```

0.5 s is a reasonable natural-image presentation duration for 2P pilots and is close to Stringer-style passive visual timing.

#### `ITI_MODE`

Inter-trial interval mode.

```python
ITI_MODE = "fixed"
```

Allowed values:

- `"fixed"`: same gray-screen duration every trial.
- `"jittered"`: gray-screen duration sampled uniformly between `GRAY_DURATION_MIN` and `GRAY_DURATION_MAX`.

Pilot recommendation: use `"fixed"` first to simplify timing validation.

Main-experiment option: use `"jittered"` to reduce temporal predictability.

#### `GRAY_DURATION`

Gray-screen duration in seconds when `ITI_MODE = "fixed"`.

```python
GRAY_DURATION = 0.75
```

#### `GRAY_DURATION_MIN` and `GRAY_DURATION_MAX`

Jitter range in seconds when `ITI_MODE = "jittered"`.

```python
GRAY_DURATION_MIN = 0.5
GRAY_DURATION_MAX = 1.0
```

Each trial's actual planned gray duration is saved in `planned_sequence.csv` and `event_log.csv`.

#### `INITIAL_GRAY`

Gray screen shown before the first stimulus.

```python
INITIAL_GRAY = 5.0
```

This gives the display, photodiode trace, and acquisition some baseline time.

#### `FINAL_GRAY`

Gray screen shown after the last stimulus.

```python
FINAL_GRAY = 5.0
```

This gives a clean end-of-stimulus baseline.

#### `POST_FLIP_DELAY_SEC`

Optional delay after `pygame.display.flip()`.

```python
POST_FLIP_DELAY_SEC = 0.0
```

Usually leave this at 0. The photodiode trace is the gold standard for actual screen onset.

## GPIO / TTL variables

#### `USE_GPIO`

Whether to send TTL pulses through Raspberry Pi GPIO.

```python
USE_GPIO = True
```

Set to `False` if testing on a non-RPi computer.

#### `TTL_PIN`

BCM GPIO pin used for stimulus TTL output.

```python
TTL_PIN = 23
```

Important NeuroKairos-related note:

- Avoid GPIO 9 if your lab uses it for NeuroKairos IRIG output.
- Avoid GPIO 18 if your GPS/PPS hardware uses it.
- GPIO 23 is a reasonable stimulus TTL output candidate if it is free on your rig.

Use a shared ground between the Raspberry Pi and the 2P acquisition system.

#### `TTL_PULSE_SEC`

Duration of the TTL pulse in seconds.

```python
TTL_PULSE_SEC = 0.005
```

0.005 s means 5 ms.

## Display variables

#### `FULLSCREEN`

Whether to use fullscreen display.

```python
FULLSCREEN = True
```

Use fullscreen for real experiments.

#### `SCREEN_SIZE_OVERRIDE`

Window size used only when `FULLSCREEN = False`.

```python
SCREEN_SIZE_OVERRIDE = (1920, 1080)
```

Useful for testing.

#### `KEEP_ASPECT_RATIO`

Whether to preserve image aspect ratio.

```python
KEEP_ASPECT_RATIO = True
```

- `True`: image is scaled to fit screen and letterboxed on gray background.
- `False`: image is stretched to fill the screen.

For natural images, `True` is usually safer because it avoids geometric distortion.

#### `GRAY_COLOR`

RGB value for the gray ITI screen and image letterbox background.

```python
GRAY_COLOR = (128, 128, 128)
```

Use gray rather than black to avoid large global luminance transitions. If your display hardware maps values to blue-only OLED emission, leave this as standard software gray and document the hardware behavior.

## Photodiode variables

#### `PHOTODIODE_SIZE_PX`

Size of the square photodiode patch in pixels.

```python
PHOTODIODE_SIZE_PX = 120
```

Increase if the photodiode signal is weak or placement is difficult.

#### `PHOTODIODE_MARGIN_PX`

Distance from the screen edge in pixels.

```python
PHOTODIODE_MARGIN_PX = 0
```

A value of 0 places the patch at the top-right corner.

#### `PHOTODIODE_ON_COLOR`

Patch color during image presentation.

```python
PHOTODIODE_ON_COLOR = (255, 255, 255)
```

The display hardware may render this through blue-only emission depending on your OLED/controller.

#### `PHOTODIODE_OFF_COLOR`

Patch color during gray ITI.

```python
PHOTODIODE_OFF_COLOR = (0, 0, 0)
```

This gives the photodiode a strong transition without making the whole screen black.

## Performance variable

#### `CACHE_IMAGES`

Whether to preload each scaled image surface when first used and reuse it later.

```python
CACHE_IMAGES = True
```

For 100-300 images this is usually fine. For much larger image sets, memory usage may become high. Set to `False` to load images trial-by-trial.

## Display hardware note

#### `DISPLAY_HARDWARE_NOTE`

Free-text note stored in metadata.

```python
DISPLAY_HARDWARE_NOTE = (
    "OLED/display hardware controls blue-pixel emission to reduce PMT contamination; "
    "software presents standard grayscale/RGB values."
)
```

This does not change the stimulus. It documents that the display system handles blue-only emission outside the Python script.

## Timing interpretation

The script logs timestamps immediately after `pygame.display.flip()` and TTL generation. These software times are useful, but they are not the true visual onset.

For analysis:

```text
Image identity/order: planned_sequence.csv and event_log.csv
Absolute time: unix_time_utc_sec
True visual onset: photodiode trace
Hardware alignment marker: TTL pulse
Clock reference: NeuroKairos/UTC synchronization, if used
```

Use the photodiode trace as the gold-standard measurement of when the screen actually changed.

## Expected data size

For 200 images x 10 repeats:

```text
planned_sequence.csv: usually <1 MB
event_log.csv: usually <1-2 MB
metadata.json: <20 KB
selected_images.csv: small
```

The visual stimulus logs are tiny compared with 2P imaging data.

## Common modifications

### Short hardware test

```python
N_IMAGES_TO_USE = 20
N_REPEATS = 2
STIM_DURATION = 0.5
ITI_MODE = "fixed"
GRAY_DURATION = 0.75
```

### 2P pilot

```python
N_IMAGES_TO_USE = 100
N_REPEATS = 5
STIM_DURATION = 0.5
ITI_MODE = "fixed"
GRAY_DURATION = 0.75
```

### Main decoding experiment

```python
N_IMAGES_TO_USE = 200
N_REPEATS = 10
STIM_DURATION = 0.5
ITI_MODE = "jittered"
GRAY_DURATION_MIN = 0.5
GRAY_DURATION_MAX = 1.0
```

## Troubleshooting

### The script cannot find images

Check `IMAGE_DIR` and verify PNG files exist:

```bash
ls /home/pi/vstim_natural/stringer_natimg2800_center_crop_png | head
```

### GPIO error on laptop

Set:

```python
USE_GPIO = False
```

### Escape from fullscreen

Press `Esc`. The script should log `session_interrupted` and `program_exit`.

### Photodiode signal is weak

Try increasing:

```python
PHOTODIODE_SIZE_PX = 180
```

Also confirm the photodiode is physically positioned over the top-right patch.

### Timing looks off

Use the photodiode trace, not the software log alone, to estimate actual visual onset. Software logging, TTL, and screen refresh can differ by a few milliseconds or more depending on display and OS behavior.
