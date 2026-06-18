#!/usr/bin/env python3
"""
run_stringer_vstim.py

Natural-image visual stimulus presentation for a Raspberry Pi 2P rig.

Main features
-------------
- Asks for mouse ID at runtime.
- Selects a fixed-size subset of natural images from a local PNG library.
- Repeats each selected image N_REPEATS times.
- Randomizes trial order while avoiding immediate repeats when possible.
- Presents images fullscreen with a gray ITI screen.
- Draws a high-contrast photodiode square in the top-right corner.
- Sends a TTL pulse at stimulus onset through Raspberry Pi GPIO.
- Saves planned sequence, event log with Unix UTC timestamps, and metadata.

Assumptions
-----------
- The Raspberry Pi clock is synchronized to UTC, e.g. with NeuroKairos.
- The image library has already been converted to PNGs.
- Display hardware handles any blue-only OLED emission if applicable.

Tested conceptually for pygame-based stimulus presentation. Always run a
hardware pilot with photodiode and oscilloscope/acquisition trace before real data.
"""

import csv
import json
import os
import random
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ============================================================
# USER SETTINGS
# Edit these values before running the experiment.
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

# Folder containing natural image PNG files.
# Example files: natimg_0000.png, natimg_0001.png, ...
# If you copy this repo to /home/pi/vstim_natural, this resolves to:
# /home/pi/vstim_natural/stringer_natimg2800_center_crop_png
IMAGE_DIR = SCRIPT_DIR / "stringer_natimg2800_center_crop_png"

# Root output folder. The script will create:
# OUTPUT_ROOT / mouse_id / session_id / logs...
OUTPUT_ROOT = Path("/home/pi/stim_logs")


# Number of unique images to use from IMAGE_DIR.
# For pilot: 100 is reasonable.
# For main experiment: 200-300 is reasonable.
# Use None to use all available images, but this is usually too long.
N_IMAGES_TO_USE = 5

# Number of times each selected image is shown.
# Pilot suggestion: 5-10.
# Main experiment suggestion: 8-10 for 200 images, or 6 for 300 images.
N_REPEATS = 2

# Duration of image presentation in seconds.
# Stringer-style natural image presentation is often around 0.5 s.
STIM_DURATION = 0.5

# ITI mode: "fixed" or "jittered".
# Pilot recommendation: "fixed".
# Main experiment can use "jittered" after timing is validated.
ITI_MODE = "fixed"

# Fixed gray-screen duration in seconds, used when ITI_MODE == "fixed".
GRAY_DURATION = 0.75

# Jittered gray-screen duration range in seconds, used when ITI_MODE == "jittered".
GRAY_DURATION_MIN = 0.5
GRAY_DURATION_MAX = 1.0

# Initial and final gray screen durations in seconds.
INITIAL_GRAY = 5.0
FINAL_GRAY = 5.0

# Seed for choosing the image subset.
# Keep this fixed across animals/sessions if you want the same image subset.
IMAGE_SUBSET_SEED = 777

# Seed for randomized trial order.
# Use None for a fresh random order each session.
# Use an integer for exactly reproducible trial order.
TRIAL_ORDER_SEED = None

# Try to prevent the same image from appearing on adjacent trials.
AVOID_ADJACENT_REPEATS = True

# Raspberry Pi GPIO TTL settings.
# IMPORTANT: avoid GPIO 9 if NeuroKairos uses it for IRIG output.
# Avoid GPIO 18 if your GPS/PPS hardware uses it.
USE_GPIO = False
TTL_PIN = 23          # BCM GPIO numbering; GPIO 23 is physical pin 16.
TTL_PULSE_SEC = 0.005 # 5 ms pulse.

# Display settings.
FULLSCREEN = True
SCREEN_SIZE_OVERRIDE = (1280, 720)  # Used only if FULLSCREEN = False.
KEEP_ASPECT_RATIO = True             # Letterbox images on gray background.

# Display-routing settings.
# When the script is launched over SSH with X11 forwarding, DISPLAY often points
# to the forwarded session (for example localhost:11.0). In that case, pygame
# opens on the remote machine instead of the behavior Pi HDMI display.
#
# This script defaults to the Pi's local desktop X server. If the behavior Pi is
# showing a Linux console instead of a desktop session, try:
#   DISPLAY_TARGET = None
#   SDL_VIDEODRIVER_TARGET = "kmsdrm"
FORCE_LOCAL_DISPLAY_WHEN_SSH = True
DISPLAY_TARGET = ":0"
SDL_VIDEODRIVER_TARGET = "x11"
XAUTHORITY_TARGET = None  # None -> auto-use ~/.Xauthority when targeting local X11.

# Screen colors. If your OLED hardware maps RGB to blue-only emission,
# leave these as standard grayscale/RGB values and document that in metadata.
GRAY_COLOR = (128, 128, 128)

# Photodiode patch settings.
# Patch is bright during image presentation and dark during gray ITI.
PHOTODIODE_SIZE_PX = 120
PHOTODIODE_MARGIN_PX = 0
PHOTODIODE_ON_COLOR = (255, 255, 255)
PHOTODIODE_OFF_COLOR = (0, 0, 0)

# Optional short stabilization delay after each flip before logging/sleeping.
# Usually leave this at 0.0; photodiode gives true screen timing.
POST_FLIP_DELAY_SEC = 0.0

# Whether to cache loaded/rescaled image surfaces in memory.
# For 100-300 images this is usually fine. For much larger sets, set False.
CACHE_IMAGES = True

# Metadata note about display hardware.
DISPLAY_HARDWARE_NOTE = (
    "OLED/display hardware controls blue-pixel emission to reduce PMT contamination; "
    "software presents standard grayscale/RGB values."
)


# ============================================================
# Utility functions
# ============================================================

pygame = None


def is_forwarded_x11_display(display_value: str) -> bool:
    """Return True when DISPLAY looks like an SSH-forwarded X11 session."""
    if not display_value:
        return False

    normalized = display_value.lower()
    if normalized.startswith("localhost/unix:"):
        return True

    host = normalized.split(":", 1)[0]
    return host in {"localhost", "127.0.0.1", "::1"}


def configure_display_environment():
    """
    Configure which display pygame/SDL should use.

    This must run before importing pygame or initializing the video subsystem.
    It is mainly needed when launching over SSH because DISPLAY may point to an
    X11-forwarded remote display instead of the behavior Pi's physical screen.
    """
    original_display = os.environ.get("DISPLAY")
    original_sdl_driver = os.environ.get("SDL_VIDEODRIVER")
    original_xauthority = os.environ.get("XAUTHORITY")
    forwarded_display = is_forwarded_x11_display(original_display)
    should_force_local = FORCE_LOCAL_DISPLAY_WHEN_SSH and forwarded_display

    if SDL_VIDEODRIVER_TARGET is not None and (should_force_local or not original_sdl_driver):
        os.environ["SDL_VIDEODRIVER"] = SDL_VIDEODRIVER_TARGET

    effective_sdl_driver = os.environ.get("SDL_VIDEODRIVER")

    if DISPLAY_TARGET is not None and (
        should_force_local or
        (not original_display and effective_sdl_driver in (None, "", "x11"))
    ):
        os.environ["DISPLAY"] = DISPLAY_TARGET

    # Console/KMS SDL backends should not inherit a forwarded X11 or Wayland session.
    if effective_sdl_driver in {"kmsdrm", "fbcon", "directfb"}:
        os.environ.pop("DISPLAY", None)
        os.environ.pop("WAYLAND_DISPLAY", None)

    xauthority_target = XAUTHORITY_TARGET
    if xauthority_target is None:
        local_xauthority = Path.home() / ".Xauthority"
        if os.environ.get("DISPLAY") == DISPLAY_TARGET and local_xauthority.exists():
            xauthority_target = str(local_xauthority)

    if xauthority_target is not None and (should_force_local or not original_xauthority):
        os.environ["XAUTHORITY"] = xauthority_target

    info = {
        "original_display": original_display,
        "original_sdl_videodriver": original_sdl_driver,
        "original_xauthority": original_xauthority,
        "forwarded_x11_detected": forwarded_display,
        "force_local_display_when_ssh": FORCE_LOCAL_DISPLAY_WHEN_SSH,
        "effective_display": os.environ.get("DISPLAY"),
        "effective_sdl_videodriver": os.environ.get("SDL_VIDEODRIVER"),
        "effective_xauthority": os.environ.get("XAUTHORITY"),
    }

    print("Display environment:")
    print(f"  original DISPLAY={info['original_display']}")
    print(f"  original SDL_VIDEODRIVER={info['original_sdl_videodriver']}")
    print(f"  original XAUTHORITY={info['original_xauthority']}")
    print(f"  forwarded_x11_detected={info['forwarded_x11_detected']}")
    print(f"  effective DISPLAY={info['effective_display']}")
    print(f"  effective SDL_VIDEODRIVER={info['effective_sdl_videodriver']}")
    print(f"  effective XAUTHORITY={info['effective_xauthority']}")

    return info


def sanitize_id(text: str) -> str:
    """Keep file-system-safe mouse/session text."""
    text = text.strip()
    keep = []
    for c in text:
        if c.isalnum() or c in ["-", "_"]:
            keep.append(c)
        else:
            keep.append("_")
    return "".join(keep)


def utc_iso_now() -> str:
    """Current UTC time as ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def unix_time() -> float:
    """Current Unix UTC time in seconds."""
    return time.time()


def choose_image_subset(all_image_files, n_images_to_use, seed):
    """Choose a reproducible subset of image files."""
    if n_images_to_use is None:
        return sorted(all_image_files)

    if n_images_to_use > len(all_image_files):
        raise RuntimeError(
            f"N_IMAGES_TO_USE={n_images_to_use}, but only "
            f"{len(all_image_files)} images found in IMAGE_DIR."
        )

    rng = random.Random(seed)
    image_files = rng.sample(all_image_files, n_images_to_use)
    return sorted(image_files)


def make_random_sequence(image_files, n_repeats, seed=None, avoid_adjacent=True):
    """Create randomized trial list with n_repeats per image."""
    rng = random.Random(seed)

    trials = []
    for repeat in range(n_repeats):
        for image_id, path in enumerate(image_files):
            trials.append({
                "image_id": image_id,
                "image_filename": path.name,
                "repeat_number": repeat + 1,
            })

    if not avoid_adjacent:
        rng.shuffle(trials)
        return trials

    # Repeated shuffling is sufficient when many unique images are used.
    for _ in range(1000):
        rng.shuffle(trials)
        ok = all(
            trials[i]["image_id"] != trials[i - 1]["image_id"]
            for i in range(1, len(trials))
        )
        if ok:
            return trials

    print("Warning: could not fully avoid adjacent repeated images.", file=sys.stderr)
    return trials


def sample_gray_duration(rng):
    """Return ITI duration for this trial."""
    if ITI_MODE == "fixed":
        return float(GRAY_DURATION)
    if ITI_MODE == "jittered":
        return float(rng.uniform(GRAY_DURATION_MIN, GRAY_DURATION_MAX))
    raise ValueError(f"Unknown ITI_MODE: {ITI_MODE}. Use 'fixed' or 'jittered'.")


def photodiode_rect(screen_size):
    """Top-right photodiode rectangle."""
    w, _ = screen_size
    x = w - PHOTODIODE_SIZE_PX - PHOTODIODE_MARGIN_PX
    y = PHOTODIODE_MARGIN_PX
    return pygame.Rect(x, y, PHOTODIODE_SIZE_PX, PHOTODIODE_SIZE_PX)


def add_photodiode_patch(surface, color):
    """Draw photodiode patch on a pygame surface."""
    pygame.draw.rect(surface, color, photodiode_rect(surface.get_size()))
    return surface


def make_gray_screen(screen_size, photodiode_on=False):
    """Create gray screen with black or bright photodiode patch."""
    surf = pygame.Surface(screen_size)
    surf.fill(GRAY_COLOR)
    color = PHOTODIODE_ON_COLOR if photodiode_on else PHOTODIODE_OFF_COLOR
    add_photodiode_patch(surf, color)
    return surf


def load_image_as_stimulus(image_path, screen_size):
    """
    Load an image and return a full-screen stimulus surface.
    Image is either stretched or letterboxed, then photodiode patch is added.
    """
    img = pygame.image.load(str(image_path)).convert()
    screen_w, screen_h = screen_size

    if KEEP_ASPECT_RATIO:
        img_rect = img.get_rect()
        scale = min(screen_w / img_rect.width, screen_h / img_rect.height)
        new_size = (int(img_rect.width * scale), int(img_rect.height * scale))
        img = pygame.transform.smoothscale(img, new_size)

        canvas = pygame.Surface(screen_size)
        canvas.fill(GRAY_COLOR)
        x = (screen_w - new_size[0]) // 2
        y = (screen_h - new_size[1]) // 2
        canvas.blit(img, (x, y))
    else:
        canvas = pygame.transform.smoothscale(img, screen_size)

    add_photodiode_patch(canvas, PHOTODIODE_ON_COLOR)
    return canvas


def write_csv(path, rows, fieldnames):
    """Write a list of dict rows to CSV."""
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def append_csv_row(path, row, fieldnames):
    """Append one row to CSV, creating header if needed."""
    file_exists = path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fieldnames})


# ============================================================
# Main experiment function
# ============================================================

def main():
    display_env_info = configure_display_environment()

    global pygame
    import pygame as pygame_module
    pygame = pygame_module

    # Validate ITI config early.
    if ITI_MODE not in ["fixed", "jittered"]:
        raise RuntimeError("ITI_MODE must be either 'fixed' or 'jittered'.")
    if ITI_MODE == "jittered" and GRAY_DURATION_MIN > GRAY_DURATION_MAX:
        raise RuntimeError("GRAY_DURATION_MIN cannot be greater than GRAY_DURATION_MAX.")

    mouse_id = sanitize_id(input("Mouse ID: "))
    if not mouse_id:
        raise RuntimeError("Mouse ID cannot be empty.")
    session_notes = input("Session notes, optional: ").strip()

    # Fresh randomized order seed if not manually specified.
    actual_trial_order_seed = TRIAL_ORDER_SEED
    if actual_trial_order_seed is None:
        actual_trial_order_seed = int(time.time_ns() % (2 ** 32))

    iti_rng = random.Random(actual_trial_order_seed + 1)

    session_start_unix = unix_time()
    session_start_iso_utc = utc_iso_now()
    session_start_label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_id = f"{mouse_id}_{session_start_label}"

    output_dir = OUTPUT_ROOT / mouse_id / session_id
    output_dir.mkdir(parents=True, exist_ok=True)

    planned_sequence_csv = output_dir / f"{session_id}_planned_sequence.csv"
    event_log_csv = output_dir / f"{session_id}_event_log.csv"
    metadata_json = output_dir / f"{session_id}_metadata.json"
    selected_images_csv = output_dir / f"{session_id}_selected_images.csv"

    all_image_files = sorted(IMAGE_DIR.glob("*.png"))
    if len(all_image_files) == 0:
        raise RuntimeError(f"No PNG files found in IMAGE_DIR: {IMAGE_DIR}")

    image_files = choose_image_subset(all_image_files, N_IMAGES_TO_USE, IMAGE_SUBSET_SEED)

    trials = make_random_sequence(
        image_files=image_files,
        n_repeats=N_REPEATS,
        seed=actual_trial_order_seed,
        avoid_adjacent=AVOID_ADJACENT_REPEATS,
    )

    # Add trial-level planned fields.
    for i, t in enumerate(trials):
        t["trial_index"] = i
        t["planned_stim_duration_sec"] = STIM_DURATION
        t["planned_gray_duration_sec"] = sample_gray_duration(iti_rng)

    planned_fields = [
        "trial_index",
        "image_id",
        "image_filename",
        "repeat_number",
        "planned_stim_duration_sec",
        "planned_gray_duration_sec",
    ]
    write_csv(planned_sequence_csv, trials, planned_fields)

    selected_rows = [
        {"image_id": i, "image_filename": path.name, "image_path": str(path)}
        for i, path in enumerate(image_files)
    ]
    write_csv(selected_images_csv, selected_rows, ["image_id", "image_filename", "image_path"])

    event_fields = [
        "session_id",
        "mouse_id",
        "event_type",
        "trial_index",
        "image_id",
        "image_filename",
        "repeat_number",
        "unix_time_utc_sec",
        "perf_counter_sec",
        "iso_time_utc",
        "ttl_sent",
        "photodiode_state",
        "planned_stim_duration_sec",
        "planned_gray_duration_sec",
        "notes",
    ]

    metadata = {
        "session_id": session_id,
        "mouse_id": mouse_id,
        "session_notes": session_notes,
        "host": socket.gethostname(),
        "session_start_unix_utc_sec": session_start_unix,
        "session_start_iso_utc": session_start_iso_utc,
        "image_dir": str(IMAGE_DIR),
        "output_dir": str(output_dir),
        "selected_images_csv": str(selected_images_csv),
        "planned_sequence_csv": str(planned_sequence_csv),
        "event_log_csv": str(event_log_csv),
        "display_env_info": display_env_info,
        "n_total_available_images": len(all_image_files),
        "n_images_used": len(image_files),
        "n_repeats": N_REPEATS,
        "n_trials": len(trials),
        "stim_duration_sec": STIM_DURATION,
        "iti_mode": ITI_MODE,
        "gray_duration_sec_fixed": GRAY_DURATION if ITI_MODE == "fixed" else None,
        "gray_duration_min_sec": GRAY_DURATION_MIN if ITI_MODE == "jittered" else None,
        "gray_duration_max_sec": GRAY_DURATION_MAX if ITI_MODE == "jittered" else None,
        "initial_gray_sec": INITIAL_GRAY,
        "final_gray_sec": FINAL_GRAY,
        "image_subset_seed": IMAGE_SUBSET_SEED,
        "trial_order_seed": actual_trial_order_seed,
        "avoid_adjacent_repeats": AVOID_ADJACENT_REPEATS,
        "use_gpio": USE_GPIO,
        "ttl_pin_bcm": TTL_PIN if USE_GPIO else None,
        "ttl_pulse_sec": TTL_PULSE_SEC if USE_GPIO else None,
        "fullscreen": FULLSCREEN,
        "screen_size_override": SCREEN_SIZE_OVERRIDE,
        "keep_aspect_ratio": KEEP_ASPECT_RATIO,
        "gray_color": GRAY_COLOR,
        "photodiode_size_px": PHOTODIODE_SIZE_PX,
        "photodiode_margin_px": PHOTODIODE_MARGIN_PX,
        "photodiode_location": "top_right",
        "photodiode_on_color": PHOTODIODE_ON_COLOR,
        "photodiode_off_color": PHOTODIODE_OFF_COLOR,
        "post_flip_delay_sec": POST_FLIP_DELAY_SEC,
        "cache_images": CACHE_IMAGES,
        "display_hardware_note": DISPLAY_HARDWARE_NOTE,
        "timing_note": (
            "unix_time_utc_sec is logged from time.time(); photodiode trace should be "
            "used as gold-standard visual onset timing."
        ),
    }

    with open(metadata_json, "w") as f:
        json.dump(metadata, f, indent=2)

    print(f"Session ID: {session_id}")
    print(f"Images used: {len(image_files)}")
    print(f"Repeats per image: {N_REPEATS}")
    print(f"Total trials: {len(trials)}")
    print(f"Planned sequence: {planned_sequence_csv}")
    print(f"Event log: {event_log_csv}")
    print(f"Metadata: {metadata_json}")

    # GPIO setup is inside main so the script can be imported without touching GPIO.
    gpio_module = None
    if USE_GPIO:
        import RPi.GPIO as GPIO
        gpio_module = GPIO
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(TTL_PIN, GPIO.OUT)
        GPIO.output(TTL_PIN, GPIO.LOW)

    def ttl_pulse():
        if gpio_module is not None:
            gpio_module.output(TTL_PIN, gpio_module.HIGH)
            time.sleep(TTL_PULSE_SEC)
            gpio_module.output(TTL_PIN, gpio_module.LOW)

    def log_event(event_type, trial=None, ttl_sent=False, photodiode_state="", notes=""):
        row = {
            "session_id": session_id,
            "mouse_id": mouse_id,
            "event_type": event_type,
            "trial_index": trial.get("trial_index", "") if trial else "",
            "image_id": trial.get("image_id", "") if trial else "",
            "image_filename": trial.get("image_filename", "") if trial else "",
            "repeat_number": trial.get("repeat_number", "") if trial else "",
            "unix_time_utc_sec": f"{unix_time():.6f}",
            "perf_counter_sec": f"{time.perf_counter():.6f}",
            "iso_time_utc": utc_iso_now(),
            "ttl_sent": int(ttl_sent),
            "photodiode_state": photodiode_state,
            "planned_stim_duration_sec": trial.get("planned_stim_duration_sec", "") if trial else "",
            "planned_gray_duration_sec": trial.get("planned_gray_duration_sec", "") if trial else "",
            "notes": notes,
        }
        append_csv_row(event_log_csv, row, event_fields)

    pygame.init()
    try:
        if FULLSCREEN:
            screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        else:
            screen = pygame.display.set_mode(SCREEN_SIZE_OVERRIDE)

        pygame.mouse.set_visible(False)
        screen_size = screen.get_size()
        pygame_driver = pygame.display.get_driver()
        pygame_num_displays = None
        pygame_desktop_sizes = []
        if hasattr(pygame.display, "get_num_displays"):
            pygame_num_displays = pygame.display.get_num_displays()
        if hasattr(pygame.display, "get_desktop_sizes"):
            pygame_desktop_sizes = [list(size) for size in pygame.display.get_desktop_sizes()]

        print(f"Pygame display driver: {pygame_driver}")
        print(f"Pygame screen size: {screen_size}")
        if pygame_num_displays is not None:
            print(f"Pygame number of displays: {pygame_num_displays}")
        if pygame_desktop_sizes:
            print(f"Pygame desktop sizes: {pygame_desktop_sizes}")

        metadata["actual_screen_size_px"] = list(screen_size)
        metadata["pygame_display_driver"] = pygame_driver
        metadata["pygame_num_displays"] = pygame_num_displays
        metadata["pygame_desktop_sizes"] = pygame_desktop_sizes
        with open(metadata_json, "w") as f:
            json.dump(metadata, f, indent=2)

        gray_off = make_gray_screen(screen_size, photodiode_on=False)
        stimulus_cache = {}

        screen.blit(gray_off, (0, 0))
        pygame.display.flip()
        if POST_FLIP_DELAY_SEC > 0:
            time.sleep(POST_FLIP_DELAY_SEC)
        log_event(
            event_type="session_start_initial_gray",
            photodiode_state="off_black",
            notes=f"initial_gray_sec={INITIAL_GRAY}",
        )
        time.sleep(INITIAL_GRAY)

        for trial in trials:
            # Allow quitting with Escape or window close.
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    raise KeyboardInterrupt
                if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    raise KeyboardInterrupt

            image_path = IMAGE_DIR / trial["image_filename"]

            if CACHE_IMAGES:
                if image_path not in stimulus_cache:
                    stimulus_cache[image_path] = load_image_as_stimulus(image_path, screen_size)
                stim_surface = stimulus_cache[image_path]
            else:
                stim_surface = load_image_as_stimulus(image_path, screen_size)

            # Stimulus onset: draw image + bright photodiode patch.
            screen.blit(stim_surface, (0, 0))
            pygame.display.flip()
            if POST_FLIP_DELAY_SEC > 0:
                time.sleep(POST_FLIP_DELAY_SEC)

            ttl_pulse()
            log_event(
                event_type="stim_on",
                trial=trial,
                ttl_sent=True,
                photodiode_state="on_bright",
            )
            time.sleep(float(trial["planned_stim_duration_sec"]))

            # Stimulus offset / gray ITI: gray screen + dark photodiode patch.
            screen.blit(gray_off, (0, 0))
            pygame.display.flip()
            if POST_FLIP_DELAY_SEC > 0:
                time.sleep(POST_FLIP_DELAY_SEC)
            log_event(
                event_type="stim_off_gray_on",
                trial=trial,
                ttl_sent=False,
                photodiode_state="off_black",
            )
            time.sleep(float(trial["planned_gray_duration_sec"]))

        screen.blit(gray_off, (0, 0))
        pygame.display.flip()
        log_event(
            event_type="session_end_final_gray",
            photodiode_state="off_black",
            notes=f"final_gray_sec={FINAL_GRAY}",
        )
        time.sleep(FINAL_GRAY)

    except KeyboardInterrupt:
        log_event(
            event_type="session_interrupted",
            photodiode_state="unknown",
            notes="KeyboardInterrupt, Escape, or window close",
        )
        print("Session interrupted.")

    finally:
        pygame.quit()
        if gpio_module is not None:
            gpio_module.output(TTL_PIN, gpio_module.LOW)
            gpio_module.cleanup()

        log_event(event_type="program_exit", photodiode_state="unknown")
        print(f"Event log saved to: {event_log_csv}")
        print(f"Planned sequence saved to: {planned_sequence_csv}")
        print(f"Metadata saved to: {metadata_json}")


if __name__ == "__main__":
    main()
