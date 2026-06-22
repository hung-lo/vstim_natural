
#!/usr/bin/env python3
"""
run_stringer_vstim.py

Version 0 headless visual stimulus runner for the behavior Raspberry Pi.

This first cut keeps the runtime simple:
  - no photodiode patch by default
  - no precomputed raw frame pipeline
  - precompute display surfaces once at startup
  - run fullscreen on the Pi's direct display backend
  - log sequence and events to CSV/JSON

The goal is to prove that the headless Pi can actually show the images.
Once that works, we can layer photodiode support back in.

Recommended launch from a local Pi TTY:

    cd ~/vstim_natural
    source .venv/bin/activate
    unset DISPLAY
    unset SDL_VIDEODRIVER
    unset XAUTHORITY
    python3 run_stringer_vstim.py

If the direct backend needs help:

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

import pygame

PROJECT_ROOT = Path(__file__).resolve().parent

IMAGE_DIR_CANDIDATES = [
    PROJECT_ROOT / "stringer_natimg2800_center_crop_png",
    Path("/home/pi/stringer_natimg2800_center_crop_png"),
]
OUTPUT_ROOT = Path("/home/pi/stim_logs")

N_IMAGES_TO_USE = 5
N_REPEATS = 2
IMAGE_SUBSET_SEED = 777
TRIAL_ORDER_SEED = None
AVOID_ADJACENT_REPEATS = True

STIM_DURATION_SEC = 0.5
ITI_DURATION_SEC = 0.75
INITIAL_GRAY_SEC = 3.0
FINAL_GRAY_SEC = 3.0

FULLSCREEN = True
SCREEN_SIZE_OVERRIDE = (1024, 600)
KEEP_ASPECT_RATIO = True

BACKGROUND_GRAY = (128, 128, 128)
ENABLE_PHOTODIODE_PATCH = False
PHOTODIODE_SIZE_PX = 120
PHOTODIODE_MARGIN_PX = 0
PHOTODIODE_ON_COLOR = (255, 255, 255)
PHOTODIODE_OFF_COLOR = (0, 0, 0)

USE_GPIO = False
TTL_PIN_BCM = 23
TTL_PULSE_SEC = 0.005


def configure_display_environment():
    """Remove SSH/X11 display routing so pygame uses the Pi's local backend."""
    original = {
        "DISPLAY": os.environ.get("DISPLAY"),
        "SDL_VIDEODRIVER": os.environ.get("SDL_VIDEODRIVER"),
        "XAUTHORITY": os.environ.get("XAUTHORITY"),
    }

    os.environ.pop("DISPLAY", None)
    os.environ.pop("XAUTHORITY", None)

    current_driver = os.environ.get("SDL_VIDEODRIVER")
    if current_driver in {"x11", "wayland"}:
        os.environ.pop("SDL_VIDEODRIVER", None)

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
    checked = "\n".join(f"  - {candidate}" for candidate in IMAGE_DIR_CANDIDATES)
    raise RuntimeError(
        "No PNG image directory was found. Checked:\n"
        f"{checked}\n"
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


def parse_image_id_from_filename(path):
    stem = path.stem
    parts = stem.split("_")
    for part in reversed(parts):
        if part.isdigit():
            return int(part)
    return None


def select_image_subset(all_image_files):
    if N_IMAGES_TO_USE is None:
        return sorted(all_image_files)

    if N_IMAGES_TO_USE > len(all_image_files):
        raise RuntimeError(
            f"N_IMAGES_TO_USE={N_IMAGES_TO_USE}, but only {len(all_image_files)} images were found."
        )

    rng = random.Random(IMAGE_SUBSET_SEED)
    return sorted(rng.sample(list(all_image_files), N_IMAGES_TO_USE))


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
            if all(base_trials[i]["image_id"] != base_trials[i - 1]["image_id"] for i in range(1, len(base_trials))):
                break
        else:
            print("Warning: could not fully avoid adjacent repeated images.")
    else:
        trial_rng.shuffle(base_trials)

    trials = []
    for trial_index, trial in enumerate(base_trials):
        row = dict(trial)
        row["trial_index"] = trial_index
        row["planned_stim_duration_sec"] = float(STIM_DURATION_SEC)
        row["planned_iti_duration_sec"] = float(ITI_DURATION_SEC)
        trials.append(row)

    return trials, seed, iti_rng


def setup_gpio():
    if not USE_GPIO:
        return None
    try:
        import RPi.GPIO as GPIO
    except ImportError as exc:
        raise RuntimeError(
            "USE_GPIO=True, but RPi.GPIO could not be imported."
        ) from exc

    GPIO.setmode(GPIO.BCM)
    GPIO.setup(TTL_PIN_BCM, GPIO.OUT)
    GPIO.output(TTL_PIN_BCM, GPIO.LOW)
    return GPIO


def ttl_pulse(GPIO):
    if GPIO is None:
        return
    GPIO.output(TTL_PIN_BCM, GPIO.HIGH)
    time.sleep(TTL_PULSE_SEC)
    GPIO.output(TTL_PIN_BCM, GPIO.LOW)


def photodiode_rect(screen_size):
    width, _ = screen_size
    return pygame.Rect(
        width - PHOTODIODE_SIZE_PX - PHOTODIODE_MARGIN_PX,
        PHOTODIODE_MARGIN_PX,
        PHOTODIODE_SIZE_PX,
        PHOTODIODE_SIZE_PX,
    )


def draw_photodiode_patch(surface, color):
    pygame.draw.rect(surface, color, photodiode_rect(surface.get_size()))


def make_gray_surface(screen_size, photodiode_on=False):
    surface = pygame.Surface(screen_size)
    surface.fill(BACKGROUND_GRAY)
    if ENABLE_PHOTODIODE_PATCH:
        draw_photodiode_patch(surface, PHOTODIODE_ON_COLOR if photodiode_on else PHOTODIODE_OFF_COLOR)
    return surface


def make_image_surface(image_path, screen_size):
    img = pygame.image.load(str(image_path)).convert()
    screen_w, screen_h = screen_size

    if KEEP_ASPECT_RATIO:
        img_rect = img.get_rect()
        scale = min(screen_w / img_rect.width, screen_h / img_rect.height)
        new_size = (
            max(1, int(img_rect.width * scale)),
            max(1, int(img_rect.height * scale)),
        )
        img = pygame.transform.smoothscale(img, new_size)
        canvas = pygame.Surface(screen_size)
        canvas.fill(BACKGROUND_GRAY)
        x = (screen_w - new_size[0]) // 2
        y = (screen_h - new_size[1]) // 2
        canvas.blit(img, (x, y))
    else:
        canvas = pygame.transform.smoothscale(img, screen_size)

    if ENABLE_PHOTODIODE_PATCH:
        draw_photodiode_patch(canvas, PHOTODIODE_ON_COLOR)
    return canvas


def check_for_quit(pygame_module):
    for event in pygame_module.event.get():
        if event.type == pygame_module.QUIT:
            return True
        if event.type == pygame_module.KEYDOWN and event.key == pygame_module.K_ESCAPE:
            return True
    return False


def sleep_with_events(pygame_module, seconds):
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        if check_for_quit(pygame_module):
            raise KeyboardInterrupt
        time.sleep(0.01)


def main():
    display_env_info = configure_display_environment()
    image_dir = resolve_image_dir()

    GPIO = setup_gpio()
    mouse_id = sanitize_id(input("Mouse ID: "))
    if not mouse_id:
        raise RuntimeError("Mouse ID cannot be empty.")
    session_notes = input("Session notes, optional: ").strip()

    actual_trial_order_seed = TRIAL_ORDER_SEED if TRIAL_ORDER_SEED is not None else int(time.time_ns() % (2 ** 32))
    session_start_unix = unix_time()
    session_start_iso_utc = utc_iso_now()
    session_id = f"{mouse_id}_{utc_session_label()}"

    output_dir = OUTPUT_ROOT / mouse_id / session_id
    output_dir.mkdir(parents=True, exist_ok=True)

    selected_images_csv = output_dir / f"{session_id}_selected_images.csv"
    planned_sequence_csv = output_dir / f"{session_id}_planned_sequence.csv"
    event_log_csv = output_dir / f"{session_id}_event_log.csv"
    metadata_json = output_dir / f"{session_id}_metadata.json"

    all_image_files = sorted(image_dir.glob("*.png"))
    if not all_image_files:
        raise RuntimeError(f"No PNG images found in resolved image directory: {image_dir}")

    selected_image_files = select_image_subset(all_image_files)
    trials, actual_trial_order_seed, iti_rng = make_trial_sequence(selected_image_files)

    selected_rows = []
    for selected_index, path in enumerate(selected_image_files):
        parsed_image_id = parse_image_id_from_filename(path)
        selected_rows.append({
            "selected_index": selected_index,
            "image_id": parsed_image_id if parsed_image_id is not None else selected_index,
            "image_filename": path.name,
            "image_path": str(path),
        })
    write_csv(selected_images_csv, selected_rows, ["selected_index", "image_id", "image_filename", "image_path"])

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
        "planned_stim_duration_sec",
        "planned_iti_duration_sec",
        "notes",
    ]

    metadata = {
        "software_mode": "v0_display_only",
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
        "stim_duration_sec": STIM_DURATION_SEC,
        "iti_duration_sec": ITI_DURATION_SEC,
        "initial_gray_sec": INITIAL_GRAY_SEC,
        "final_gray_sec": FINAL_GRAY_SEC,
        "fullscreen": FULLSCREEN,
        "screen_size_override": SCREEN_SIZE_OVERRIDE,
        "keep_aspect_ratio": KEEP_ASPECT_RATIO,
        "background_gray": BACKGROUND_GRAY,
        "photodiode_enabled": ENABLE_PHOTODIODE_PATCH,
        "photodiode_size_px": PHOTODIODE_SIZE_PX,
        "photodiode_margin_px": PHOTODIODE_MARGIN_PX,
        "photodiode_on_color": PHOTODIODE_ON_COLOR,
        "photodiode_off_color": PHOTODIODE_OFF_COLOR,
        "use_gpio": USE_GPIO,
        "ttl_pin_bcm": TTL_PIN_BCM if USE_GPIO else None,
        "ttl_pulse_sec": TTL_PULSE_SEC if USE_GPIO else None,
        "effective_display": os.environ.get("DISPLAY"),
        "effective_sdl_videodriver": os.environ.get("SDL_VIDEODRIVER"),
        "effective_xauthority": os.environ.get("XAUTHORITY"),
        "display_hardware_note": (
            "Headless behavior Pi; use the direct local display backend and run from a TTY."
        ),
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
    flags = pygame.DOUBLEBUF | (pygame.FULLSCREEN if FULLSCREEN else 0)
    if FULLSCREEN:
        screen = pygame.display.set_mode((0, 0), flags)
    else:
        screen = pygame.display.set_mode(SCREEN_SIZE_OVERRIDE, flags)
    pygame.display.set_caption("Stringer natural image VStim v0")
    pygame.mouse.set_visible(False)

    screen_size = screen.get_size()
    print(f"Pygame display driver: {pygame.display.get_driver()}")
    print(f"Pygame screen size: {screen_size}")
    try:
        print(f"Pygame number of displays: {pygame.display.get_num_displays()}")
        print(f"Pygame desktop sizes: {pygame.display.get_desktop_sizes()}")
    except Exception as exc:
        print(f"Pygame display query failed: {exc}")

    metadata["actual_screen_size_px"] = list(screen_size)
    metadata["pygame_display_driver"] = pygame.display.get_driver()
    with open(metadata_json, "w") as handle:
        json.dump(metadata, handle, indent=2)

    gray_surface = make_gray_surface(screen_size, photodiode_on=False)
    stimulus_cache = {}
    if True:
        print("Precomputing selected images...")
        for path in selected_image_files:
            stimulus_cache[path.name] = make_image_surface(path, screen_size)
        print(f"Cached {len(stimulus_cache)} images.")

    def log_event(event_type, trial=None, ttl_sent=False, photodiode_state="", notes=""):
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
            "planned_stim_duration_sec": trial.get("planned_stim_duration_sec", "") if trial else "",
            "planned_iti_duration_sec": trial.get("planned_iti_duration_sec", "") if trial else "",
            "notes": notes,
        }
        append_csv_row(event_log_csv, row, event_fields)

    try:
        screen.blit(gray_surface, (0, 0))
        pygame.display.flip()
        log_event(
            event_type="session_start_initial_gray",
            photodiode_state="none",
            notes=f"initial_gray_sec={INITIAL_GRAY_SEC}",
        )
        sleep_with_events(pygame, INITIAL_GRAY_SEC)

        for trial in trials:
            if check_for_quit(pygame):
                raise KeyboardInterrupt

            stim_surface = stimulus_cache[trial["image_filename"]]
            screen.blit(stim_surface, (0, 0))
            pygame.display.flip()
            ttl_pulse(GPIO)
            log_event(
                event_type="stim_on",
                trial=trial,
                ttl_sent=GPIO is not None,
                photodiode_state="none",
            )
            sleep_with_events(pygame, float(trial["planned_stim_duration_sec"]))

            if check_for_quit(pygame):
                raise KeyboardInterrupt

            screen.blit(gray_surface, (0, 0))
            pygame.display.flip()
            log_event(
                event_type="stim_off_gray_on",
                trial=trial,
                ttl_sent=False,
                photodiode_state="none",
                notes=f"iti_duration_sec={trial['planned_iti_duration_sec']:.6f}",
            )
            sleep_with_events(pygame, float(trial["planned_iti_duration_sec"]))

        screen.blit(gray_surface, (0, 0))
        pygame.display.flip()
        log_event(
            event_type="session_end_final_gray",
            photodiode_state="none",
            notes=f"final_gray_sec={FINAL_GRAY_SEC}",
        )
        sleep_with_events(pygame, FINAL_GRAY_SEC)

    except KeyboardInterrupt:
        log_event(
            event_type="session_interrupted",
            photodiode_state="none",
            notes="KeyboardInterrupt, Escape, or window close",
        )
        print("Session interrupted.")

    finally:
        log_event(event_type="program_exit", photodiode_state="none")
        print(f"Event log saved to: {event_log_csv}")
        print(f"Selected images saved to: {selected_images_csv}")
        print(f"Planned sequence saved to: {planned_sequence_csv}")
        print(f"Metadata saved to: {metadata_json}")
        if GPIO is not None:
            GPIO.cleanup()
        pygame.mouse.set_visible(True)
        pygame.quit()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
