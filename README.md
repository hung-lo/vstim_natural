
# Raspberry Pi Stringer Natural-Image Visual Stimulus Script

This repo now uses the lab's `rpg` framebuffer path again, not pygame.
That matters for the headless behavior Pi, because the screen is controlled
directly from the Pi framebuffer rather than through a desktop/X11 session.

Current entrypoint:

```text
run_stringer_vstim.py
```

What the runtime does:

- asks for mouse ID and optional session notes
- chooses a reproducible subset of Stringer center-crop PNGs
- bakes session-specific `rpg` raw files at startup
- displays each image fullscreen for a fixed duration
- shows gray ITI between images
- optionally bakes a photodiode patch into the frames
- logs planned sequence, actual display timestamps, and metadata

## Expected paths on the Pi

The script looks for the PNG folder in these places:

```text
~/vstim_natural/stringer_natimg2800_center_crop_png
~/stringer_natimg2800_center_crop_png
./stringer_natimg2800_center_crop_png
```

Session output goes to:

```text
~/stim_logs/<mouse_id>/<session_id>/
```

## Dependencies

The runtime expects:

- `rpg` installed from the SjulsonLab rpg repository
- `Pillow`
- `RPi.GPIO` on the Pi if GPIO output is enabled

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

## Photodiode patch

The first pass keeps the photodiode patch disabled by default so we can verify
the screen path first. To enable it, set:

```python
ENABLE_PHOTODIODE_PATCH = True
```

The helper functions already bake the patch into the session raw files.

## Notes on the display backend

The older pygame approach was a dead end for the headless behavior Pi.
The rpg path is the one the lab code already uses for framebuffer display,
and it is the right place to put this stimulus runner.
