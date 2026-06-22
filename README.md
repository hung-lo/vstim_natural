
# Raspberry Pi Stringer Natural-Image Visual Stimulus Script

This repo now has a deliberately small version 0 runtime for the headless behavior Pi.
The goal is to prove that the Pi can actually show the images before we add any more
stimulus machinery back in.

Current entrypoint:

```text
run_stringer_vstim.py
```

What v0 does:

- asks for mouse ID and optional session notes
- chooses a reproducible subset of Stringer center-crop PNGs
- precomputes the display surfaces once at startup
- shows each image fullscreen with gray ITI in between
- logs planned sequence, event timestamps, and metadata
- keeps the photodiode patch disabled by default

Why this version exists:

- the behavior Pi is headless
- X11/desktop display routing was the wrong target
- we want one simple path that can be verified from a Pi TTY first

## Files

```text
run_stringer_vstim.py   Main runnable stimulus script, v0 display-only version
fullscreen_test.py      Quick screen sanity test
README.md               This documentation
```

## Quick start

1. Copy this folder to the Raspberry Pi.
2. Make sure the PNG folder exists in one of these locations:

```text
/home/pi/vstim_natural/stringer_natimg2800_center_crop_png
/home/pi/stringer_natimg2800_center_crop_png
```

3. Install dependencies if needed:

```bash
pip install pygame
```

On a Raspberry Pi with GPIO output enabled, `RPi.GPIO` is also needed. It is often preinstalled on Raspberry Pi OS, but can usually be installed with:

```bash
pip install RPi.GPIO
```

4. Run the fullscreen test from a Pi TTY first, not from the desktop terminal:

```bash
cd ~/vstim_natural
source .venv/bin/activate
unset DISPLAY
unset SDL_VIDEODRIVER
unset XAUTHORITY
python3 fullscreen_test.py
```

5. If that works, run the stimulus script the same way:

```bash
cd ~/vstim_natural
source .venv/bin/activate
unset DISPLAY
unset SDL_VIDEODRIVER
unset XAUTHORITY
python3 run_stringer_vstim.py
```

6. Enter the mouse ID when prompted.

## Headless Pi notes

The behavior Pi should be treated as a direct-display machine, not a normal X11 desktop.
If you launch from the desktop terminal, pygame may initialize but never actually own
the physical framebuffer. The safer test is a local TTY session.

If the direct backend needs help, try:

```bash
SDL_VIDEODRIVER=RPI python3 fullscreen_test.py
SDL_VIDEODRIVER=RPI python3 run_stringer_vstim.py
```

The v0 script currently keeps the photodiode patch disabled by default. That is on
purpose. Once the basic image path is confirmed on the Pi, we can either:

- turn the patch back on in software, or
- move to a precomputed raw-frame pipeline.

## Image subset variables

#### `N_IMAGES_TO_USE`

Number of unique images selected from `IMAGE_DIR`.

```python
N_IMAGES_TO_USE = 5
```

Use a smaller number for the first display check. Suggested values:

| Use case | Suggested value |
|---|---:|
| Display smoke test | 5 |
| Hardware pilot | 20-50 |
| 2P pilot | 100 |
| Main decoding experiment | 200-300 |

## Output files

Each run creates a new session folder:

```text
OUTPUT_ROOT / mouse_id / session_id /
```

Inside the session folder:

- `selected_images.csv`
- `planned_sequence.csv`
- `event_log.csv`
- `metadata.json`

## Paper note

The uploaded Nuñez-Ochoa et al. 2026 paper is useful as a stimulus-design reference.
It confirms the 270-degree panorama origin of the Stringer-style stimuli and supports
the center-crop decision for a one-screen setup. It is not especially useful as a runtime
implementation guide for the Pi.
