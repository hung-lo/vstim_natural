#!/usr/bin/env python3
"""
run_stringer_vstim.py

Natural image visual stimulus presentation for Raspberry Pi behavior/2P rigs.

Features:
    - Presents repeated randomized natural images with gray ITI.
    - Saves selected image subset CSV.
    - Saves planned sequence CSV.
    - Saves actual event log CSV with Unix UTC timestamps.
    - Saves metadata JSON.
    - Optional GPIO TTL pulse at stimulus onset.
    - Photodiode patch in top-right corner:
        stimulus on: bright patch
        ITI/gray: dark patch
    - Designed for headless Raspberry Pi display use.

For a headless behavior Pi, do not force X11. Run from the Pi console/TTY or a
session that can take over the direct display backend:

    cd /home/pi/vstim_natural
    source .venv/bin/activate
    unset DISPLAY
    unset SDL_VIDEODRIVER
    python3 run_stringer_vstim.py

If the Pi still needs an explicit SDL backend, try:

    SDL_VIDEODRIVER=RPI python3 run_stringer_vstim.py
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
# Display environment settings
# ============================================================

# For the headless behavior Pi, keep these as None so pygame does not inherit
# SSH X forwarding or a nonexistent X11 desktop.
#
# If the direct backend does not attach automatically on the Pi, try:
#   SDL_VIDEODRIVER_TARGET = "RPI"
DISPLAY_TARGET = None
SDL_VIDEODRIVER_TARGET = None
XAUTHORITY_TARGET = None


# ============================================================
# Paths
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

# Prefer images that live alongside the repo on the Pi, but also support the
# original lab path used during debugging.
IMAGE_DIR_CANDIDATES = [
    SCRIPT_DIR / "stringer_natimg2800_center_crop_png",
    Path("/home/pi/stringer_natimg2800_center_crop_png"),
]
OUTPUT_ROOT = Path("/home/pi/stim_logs")


# ============================================================
# Experiment design
# ============================================================

# Debug defaults. For a real pilot, consider:
#   N_IMAGES_TO_USE = 100
#   N_REPEATS = 5
N_IMAGES_TO_USE = 5
N_REPEATS = 2

# Fixed image subset across sessions if IMAGE_SUBSET_SEED is fixed.
IMAGE_SUBSET_SEED = 777

# Trial order seed.
# None gives a fresh random order every run.
# Set an integer for exact reproducibility.
TRIAL_ORDER_SEED = None

AVOID_ADJACENT_REPEATS = True


# ============================================================
# Timing
# ============================================================

STIM_DURATION = 0.5

# ITI_MODE options:
#   "fixed"
#   "jitter" or "jittered"
ITI_MODE = "fixed"

# Used when ITI_MODE == "fixed"
GRAY_DURATION = 0.75

# Used when ITI_MODE == "jitter" or "jittered"
GRAY_DURATION_MIN = 0.5
GRAY_DURATION_MAX = 1.0

INITIAL_GRAY = 3.0
FINAL_GRAY = 3.0


# ============================================================
# Display / visual stimulus settings
# ============================================================

FULLSCREEN = True
SCREEN_SIZE_OVERRIDE = (1024, 600)

KEEP_ASPECT_RATIO = True

GRAY_COLOR = (128, 128, 128)

PHOTODIODE_SIZE_PX = 120
PHOTODIODE_MARGIN_PX = 0
PHOTODIODE_ON_COLOR = (255, 255, 255)
PHOTODIODE_OFF_COLOR = (0, 0, 0)

# If True, each selected image is loaded/scaled once and cached.
CACHE_IMAGES = True

DISPLAY_HARDWARE_NOTE = (
    "Behavior Pi is headless; pygame should use the direct Raspberry Pi display backend "
    "instead of X11 forwarding."
)


# ============================================================
# GPIO / TTL settings
# ============================================================

USE_GPIO = False

# Avoid GPIO 9 if NeuroKairos IRIG is using it.
# Avoid GPIO 18 if GPS/PPS uses it.
TTL_PIN = 23
TTL_PULSE_SEC = 0.005


# ============================================================
# Helper functions
# ============================================================

def configure_display_environment():
    """
    Configure pygame/SDL display environment.

    For headless Pi mode, DISPLAY_TARGET=None removes DISPLAY so pygame does
    not use SSH X forwarding. SDL_VIDEODRIVER_TARGET=None lets pygame choose
    the direct Raspberry Pi backend seen on the behavior box.
    """
    original = {
        "DISPLAY": os.environ.get("DISPLAY"),
        "SDL_VIDEODRIVER": os.environ.get("SDL_VIDEODRIVER"),
        "XAUTHORITY": os.environ.get("XAUTHORITY"),
    }

    if DISPLAY_TARGET is None:
        os.environ.pop("DISPLAY", None)
    else:
        os.environ["DISPLAY"] = DISPLAY_TARGET

    if SDL_VIDEODRIVER_TARGET is None:
        os.environ.pop("SDL_VIDEODRIVER", None)
    else:
        os.environ["SDL_VIDEODRIVER"] = SDL_VIDEODRIVER_TARGET

    if XAUTHORITY_TARGET is None:
        os.environ.pop("XAUTHORITY", None)
    else:
        os.environ["XAUTHORITY"] = XAUTHORITY_TARGET

    info = {
        "original_display": original["DISPLAY"],
        "original_sdl_videodriver": original["SDL_VIDEODRIVER"],
        "original_xauthority": original["XAUTHORITY"],
        "effective_display": os.environ.get("DISPLAY"),
        "effective_sdl_videodriver": os.environ.get("SDL_VIDEODRIVER"),
        "effective_xauthority": os.environ.get("XAUTHORITY"),
    }

    print("Display environment:")
    print(f"  original DISPLAY={info['original_display']}")
    print(f"  original SDL_VIDEODRIVER={info['original_sdl_videodriver']}")
    print(f"  original XAUTHORITY={info['original_xauthority']}")
    print(f"  effective DISPLAY={info['effective_display']}")
    print(f"  effective SDL_VIDEODRIVER={info['effective_sdl_videodriver']}")
    print(f"  effective XAUTHORITY={info['effective_xauthority']}")
    return info


def resolve_image_dir():
    for candidate in IMAGE_DIR_CANDIDATES:
        if candidate.exists():
            return candidate

    candidate_text = "\n".join(f"  - {path}" for path in IMAGE_DIR_CANDIDATES)
    raise RuntimeError(
        "No PNG image directory was found. Checked:\n"
        f"{candidate_text}\n"
        "Copy the PNG folder into the repo or update IMAGE_DIR_CANDIDATES."
    )


def sanitize_id(text):
    text = text.strip()
    keep = []
    for char in text:
        if char.isalnum() or char in ["-", "_"]:
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep)


def utc_iso_now():
    return datetime.now(timezone.utc).isoformat()


def utc_session_label():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def unix_time():
    return time.time()


def write_csv(path, rows, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def append_csv_row(path, row, fieldnames):
    path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = path.exists()

    with open(path, "a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in fieldnames})


def get_iti_duration(rng):
    if ITI_MODE == "fixed":
        return float(GRAY_DURATION)

    if ITI_MODE in {"jitter", "jittered"}:
        return float(rng.uniform(GRAY_DURATION_MIN, GRAY_DURATION_MAX))

    raise ValueError(f"Unknown ITI_MODE: {ITI_MODE}")


def parse_image_id_from_filename(path):
    """
    Tries to parse stable numeric image ID from names like:
        natimg_center_0000.png
        natimg_0000.png

    Falls back to None if parsing fails.
    """
    stem = path.stem
    parts = stem.split("_")
    for part in reversed(parts):
        if part.isdigit():
            return int(part)
    return None


def select_image_subset(all_image_files):
    if N_IMAGES_TO_USE is None:
        selected = list(all_image_files)
    else:
        if N_IMAGES_TO_USE > len(all_image_files):
            raise RuntimeError(
                f"N_IMAGES_TO_USE={N_IMAGES_TO_USE}, but only "
                f"{len(all_image_files)} images found in the resolved image directory."
            )
        subset_rng = random.Random(IMAGE_SUBSET_SEED)
        selected = subset_rng.sample(list(all_image_files), N_IMAGES_TO_USE)
        selected = sorted(selected)

    return selected


def make_trial_sequence(selected_image_files):
    if TRIAL_ORDER_SEED is None:
        seed = int(time.time_ns() % (2 ** 32))
    else:
        seed = int(TRIAL_ORDER_SEED)

    trial_rng = random.Random(seed)
    iti_rng = random.Random(seed + 1)

    base_trials = []
    for repeat_idx in range(N_REPEATS):
        for selected_index, path in enumerate(selected_image_files):
            parsed_image_id = parse_image_id_from_filename(path)
            image_id = parsed_image_id if parsed_image_id is not None else selected_index

            base_trials.append({
                "image_id": image_id,
                "selected_index": selected_index,
                "image_filename": path.name,
                "repeat_number": repeat_idx + 1,
            })

    if AVOID_ADJACENT_REPEATS:
        for _ in range(1000):
            trial_rng.shuffle(base_trials)
            ok = True
            for i in range(1, len(base_trials)):
                if base_trials[i]["image_id"] == base_trials[i - 1]["image_id"]:
                    ok = False
                    break
            if ok:
                break
        else:
            print("Warning: could not fully avoid adjacent repeated images.")
    else:
        trial_rng.shuffle(base_trials)

    trials = []
    for trial_index, trial in enumerate(base_trials):
        trial = dict(trial)
        trial["trial_index"] = trial_index
        trial["planned_stim_duration_sec"] = float(STIM_DURATION)
        trial["planned_iti_duration_sec"] = get_iti_duration(iti_rng)
        trials.append(trial)

    return trials, seed


def setup_gpio():
    if not USE_GPIO:
        return None

    try:
        import RPi.GPIO as GPIO
    except ImportError as exc:
        raise RuntimeError(
            "USE_GPIO=True, but RPi.GPIO could not be imported. "
            "Install it on the Raspberry Pi or set USE_GPIO=False."
        ) from exc

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(TTL_PIN, GPIO.OUT)
    GPIO.output(TTL_PIN, GPIO.LOW)
    return GPIO


def ttl_pulse(GPIO):
    if GPIO is None:
        return

    GPIO.output(TTL_PIN, GPIO.HIGH)
    time.sleep(TTL_PULSE_SEC)
    GPIO.output(TTL_PIN, GPIO.LOW)


def photodiode_rect(pygame, screen_size):
    width, height = screen_size
    return pygame.Rect(
        width - PHOTODIODE_SIZE_PX - PHOTODIODE_MARGIN_PX,
        PHOTODIODE_MARGIN_PX,
        PHOTODIODE_SIZE_PX,
        PHOTODIODE_SIZE_PX,
    )


def add_photodiode_patch(pygame, surface, color):
    rect = photodiode_rect(pygame, surface.get_size())
    pygame.draw.rect(surface, color, rect)


def make_gray_screen(pygame, screen_size, photodiode_on=False):
    surface = pygame.Surface(screen_size)
    surface.fill(GRAY_COLOR)

    if photodiode_on:
        add_photodiode_patch(pygame, surface, PHOTODIODE_ON_COLOR)
    else:
        add_photodiode_patch(pygame, surface, PHOTODIODE_OFF_COLOR)

    return surface


def load_image_as_stimulus(pygame, image_path, screen_size):
    image = pygame.image.load(str(image_path)).convert()
    screen_width, screen_height = screen_size

    if KEEP_ASPECT_RATIO:
        image_rect = image.get_rect()
        scale = min(
            screen_width / image_rect.width,
            screen_height / image_rect.height,
        )
        new_size = (
            max(1, int(image_rect.width * scale)),
            max(1, int(image_rect.height * scale)),
        )

        image = pygame.transform.smoothscale(image, new_size)

        canvas = pygame.Surface(screen_size)
        canvas.fill(GRAY_COLOR)

        x = (screen_width - new_size[0]) // 2
        y = (screen_height - new_size[1]) // 2
        canvas.blit(image, (x, y))
    else:
        canvas = pygame.transform.smoothscale(image, screen_size)

    add_photodiode_patch(pygame, canvas, PHOTODIODE_ON_COLOR)
    return canvas


def check_for_escape(pygame):
    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            return True
        if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
            return True
    return False


def flip_screen(pygame):
    pygame.display.flip()
    pygame.display.update()


def create_screen(pygame):
    flags = pygame.DOUBLEBUF

    if FULLSCREEN:
        flags |= pygame.FULLSCREEN
        screen = pygame.display.set_mode((0, 0), flags)
    else:
        screen = pygame.display.set_mode(SCREEN_SIZE_OVERRIDE, flags)

    pygame.display.set_caption("Stringer natural image VStim")
    pygame.mouse.set_visible(False)

    print("Pygame display:")
    try:
        print(f"  driver={pygame.display.get_driver()}")
    except Exception as exc:
        print(f"  driver query failed: {exc}")

    try:
        print(f"  num displays={pygame.display.get_num_displays()}")
    except Exception as exc:
        print(f"  num displays query failed: {exc}")

    try:
        print(f"  desktop sizes={pygame.display.get_desktop_sizes()}")
    except Exception as exc:
        print(f"  desktop sizes query failed: {exc}")

    print(f"  created screen size={screen.get_size()}")

    return screen


# ============================================================
# Main
# ============================================================

def main():
    display_env_info = configure_display_environment()
    image_dir = resolve_image_dir()

    import pygame

    GPIO = None

    try:
        GPIO = setup_gpio()

        mouse_id = sanitize_id(input("Mouse ID: "))
        if mouse_id == "":
            raise RuntimeError("Mouse ID cannot be empty.")

        session_notes = input("Session notes, optional: ").strip()

        session_start_unix = unix_time()
        session_start_iso_utc = utc_iso_now()
        session_label = utc_session_label()
        session_id = f"{mouse_id}_{session_label}"

        output_dir = OUTPUT_ROOT / mouse_id / session_id
        output_dir.mkdir(parents=True, exist_ok=True)

        selected_images_csv = output_dir / f"{session_id}_selected_images.csv"
        planned_sequence_csv = output_dir / f"{session_id}_planned_sequence.csv"
        event_log_csv = output_dir / f"{session_id}_event_log.csv"
        metadata_json = output_dir / f"{session_id}_metadata.json"

        all_image_files = sorted(image_dir.glob("*.png"))
        if len(all_image_files) == 0:
            raise RuntimeError(
                f"No PNG images found in resolved image directory: {image_dir}\n"
                "Check that the converted image folder is present on the Pi."
            )

        selected_image_files = select_image_subset(all_image_files)
        trials, actual_trial_order_seed = make_trial_sequence(selected_image_files)

        selected_rows = []
        for selected_index, path in enumerate(selected_image_files):
            parsed_image_id = parse_image_id_from_filename(path)
            selected_rows.append({
                "selected_index": selected_index,
                "image_id": parsed_image_id if parsed_image_id is not None else selected_index,
                "image_filename": path.name,
                "image_path": str(path),
            })
        write_csv(
            selected_images_csv,
            selected_rows,
            ["selected_index", "image_id", "image_filename", "image_path"],
        )

        planned_fields = [
            "trial_index",
            "image_id",
            "selected_index",
            "image_filename",
            "repeat_number",
            "planned_stim_duration_sec",
            "planned_iti_duration_sec",
        ]
        write_csv(planned_sequence_csv, trials, planned_fields)

        event_fields = [
            "session_id",
            "mouse_id",
            "event_type",
            "trial_index",
            "image_id",
            "selected_index",
            "image_filename",
            "repeat_number",
            "unix_time_utc_sec",
            "perf_counter_sec",
            "iso_time_utc",
            "ttl_sent",
            "photodiode_state",
            "notes",
        ]

        metadata = {
            "session_id": session_id,
            "mouse_id": mouse_id,
            "session_notes": session_notes,
            "host": socket.gethostname(),
            "session_start_unix_utc_sec": session_start_unix,
            "session_start_iso_utc": session_start_iso_utc,
            "image_dir": str(image_dir),
            "image_dir_candidates": [str(path) for path in IMAGE_DIR_CANDIDATES],
            "output_dir": str(output_dir),
            "selected_images_csv": str(selected_images_csv),
            "planned_sequence_csv": str(planned_sequence_csv),
            "event_log_csv": str(event_log_csv),
            "display_env_info": display_env_info,
            "n_total_available_images": len(all_image_files),
            "n_images_used": len(selected_image_files),
            "n_images_to_use": N_IMAGES_TO_USE,
            "n_repeats": N_REPEATS,
            "n_trials": len(trials),
            "image_subset_seed": IMAGE_SUBSET_SEED,
            "trial_order_seed_requested": TRIAL_ORDER_SEED,
            "trial_order_seed_actual": actual_trial_order_seed,
            "avoid_adjacent_repeats": AVOID_ADJACENT_REPEATS,
            "stim_duration_sec": STIM_DURATION,
            "iti_mode": ITI_MODE,
            "gray_duration_sec": GRAY_DURATION,
            "gray_duration_min_sec": GRAY_DURATION_MIN,
            "gray_duration_max_sec": GRAY_DURATION_MAX,
            "initial_gray_sec": INITIAL_GRAY,
            "final_gray_sec": FINAL_GRAY,
            "fullscreen": FULLSCREEN,
            "screen_size_override": SCREEN_SIZE_OVERRIDE,
            "keep_aspect_ratio": KEEP_ASPECT_RATIO,
            "gray_color": GRAY_COLOR,
            "photodiode_size_px": PHOTODIODE_SIZE_PX,
            "photodiode_margin_px": PHOTODIODE_MARGIN_PX,
            "photodiode_location": "top_right",
            "photodiode_on_color": PHOTODIODE_ON_COLOR,
            "photodiode_off_color": PHOTODIODE_OFF_COLOR,
            "cache_images": CACHE_IMAGES,
            "use_gpio": USE_GPIO,
            "ttl_pin_bcm": TTL_PIN if USE_GPIO else None,
            "ttl_pulse_sec": TTL_PULSE_SEC if USE_GPIO else None,
            "display_target": DISPLAY_TARGET,
            "sdl_videodriver_target": SDL_VIDEODRIVER_TARGET,
            "xauthority_target": XAUTHORITY_TARGET,
            "effective_display": os.environ.get("DISPLAY"),
            "effective_sdl_videodriver": os.environ.get("SDL_VIDEODRIVER"),
            "effective_xauthority": os.environ.get("XAUTHORITY"),
            "display_hardware_note": DISPLAY_HARDWARE_NOTE,
        }

        with open(metadata_json, "w") as handle:
            json.dump(metadata, handle, indent=2)

        print(f"Session ID: {session_id}")
        print(f"Resolved image directory: {image_dir}")
        print(f"Images used: {len(selected_image_files)}")
        print(f"Repeats per image: {N_REPEATS}")
        print(f"Total trials: {len(trials)}")
        print(f"Selected images: {selected_images_csv}")
        print(f"Planned sequence: {planned_sequence_csv}")
        print(f"Event log: {event_log_csv}")
        print(f"Metadata: {metadata_json}")

        pygame.init()
        screen = create_screen(pygame)
        screen_size = screen.get_size()

        metadata["actual_screen_size_px"] = list(screen_size)
        try:
            metadata["pygame_display_driver"] = pygame.display.get_driver()
        except Exception:
            metadata["pygame_display_driver"] = None

        with open(metadata_json, "w") as handle:
            json.dump(metadata, handle, indent=2)

        gray_off = make_gray_screen(pygame, screen_size, photodiode_on=False)
        stimulus_cache = {}

        if CACHE_IMAGES:
            print("Caching images...")
            for trial in trials:
                image_filename = trial["image_filename"]
                if image_filename not in stimulus_cache:
                    image_path = image_dir / image_filename
                    stimulus_cache[image_filename] = load_image_as_stimulus(
                        pygame, image_path, screen_size
                    )
            print(f"Cached {len(stimulus_cache)} images.")

        def log_event(
            event_type,
            trial=None,
            ttl_sent=False,
            photodiode_state="",
            notes="",
        ):
            row = {
                "session_id": session_id,
                "mouse_id": mouse_id,
                "event_type": event_type,
                "trial_index": trial.get("trial_index", "") if trial else "",
                "image_id": trial.get("image_id", "") if trial else "",
                "selected_index": trial.get("selected_index", "") if trial else "",
                "image_filename": trial.get("image_filename", "") if trial else "",
                "repeat_number": trial.get("repeat_number", "") if trial else "",
                "unix_time_utc_sec": f"{unix_time():.6f}",
                "perf_counter_sec": f"{time.perf_counter():.6f}",
                "iso_time_utc": utc_iso_now(),
                "ttl_sent": int(bool(ttl_sent)),
                "photodiode_state": photodiode_state,
                "notes": notes,
            }
            append_csv_row(event_log_csv, row, event_fields)

        try:
            screen.blit(gray_off, (0, 0))
            flip_screen(pygame)

            log_event(
                event_type="session_start_initial_gray",
                photodiode_state="off_black",
                notes=f"initial_gray_sec={INITIAL_GRAY}",
            )
            time.sleep(INITIAL_GRAY)

            for trial in trials:
                if check_for_escape(pygame):
                    raise KeyboardInterrupt

                image_filename = trial["image_filename"]

                if CACHE_IMAGES:
                    stim_surface = stimulus_cache[image_filename]
                else:
                    image_path = image_dir / image_filename
                    stim_surface = load_image_as_stimulus(
                        pygame, image_path, screen_size
                    )

                screen.blit(stim_surface, (0, 0))
                flip_screen(pygame)

                ttl_pulse(GPIO)
                log_event(
                    event_type="stim_on",
                    trial=trial,
                    ttl_sent=GPIO is not None,
                    photodiode_state="on_white",
                )
                time.sleep(STIM_DURATION)

                if check_for_escape(pygame):
                    raise KeyboardInterrupt

                screen.blit(gray_off, (0, 0))
                flip_screen(pygame)

                log_event(
                    event_type="stim_off_gray_on",
                    trial=trial,
                    ttl_sent=False,
                    photodiode_state="off_black",
                    notes=f"iti_duration_sec={trial['planned_iti_duration_sec']:.6f}",
                )
                time.sleep(float(trial["planned_iti_duration_sec"]))

            screen.blit(gray_off, (0, 0))
            flip_screen(pygame)

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
                notes="KeyboardInterrupt or ESC/window close",
            )
            print("Session interrupted.")

        finally:
            log_event(
                event_type="program_exit",
                photodiode_state="unknown",
            )
            print(f"Event log saved to: {event_log_csv}")
            print(f"Selected images saved to: {selected_images_csv}")
            print(f"Planned sequence saved to: {planned_sequence_csv}")
            print(f"Metadata saved to: {metadata_json}")

    finally:
        try:
            import pygame
            pygame.mouse.set_visible(True)
            pygame.quit()
        except Exception:
            pass

        if GPIO is not None:
            GPIO.cleanup()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
