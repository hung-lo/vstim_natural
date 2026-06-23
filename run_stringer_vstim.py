#!/usr/bin/env python3
"""
run_stringer_vstim.py

Headless visual stimulus runner for the behavior Raspberry Pi.
This version uses the lab's rpg framebuffer path rather than pygame.
"""

import csv
import json
import os
import random
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

PROJECT_ROOT = Path(__file__).resolve().parent
IMAGE_DIR_CANDIDATES = [
    PROJECT_ROOT / "stringer_natimg2800_center_crop_png",
    Path.home() / "vstim_natural" / "stringer_natimg2800_center_crop_png",
    Path.home() / "stringer_natimg2800_center_crop_png",
]
OUTPUT_ROOT = Path.home() / "stim_logs"

SCREEN_RESOLUTION = (1024, 600)
SCREEN_BACKGROUND_GRAY = 127
SCREEN_COLORMODE = 16
REFRESH_RATE_HZ = 60

N_IMAGES_TO_USE = 5
N_REPEATS = 2
IMAGE_SUBSET_SEED = 777
TRIAL_ORDER_SEED = None
AVOID_ADJACENT_REPEATS = True

STIM_DURATION_SEC = 0.5
ITI_DURATION_SEC = 0.75
INITIAL_GRAY_SEC = 3.0
FINAL_GRAY_SEC = 3.0

ENABLE_PHOTODIODE_PATCH = False
PHOTODIODE_SIZE_PX = 120
PHOTODIODE_MARGIN_PX = 0
PHOTODIODE_ON_COLOR = (255, 255, 255)
PHOTODIODE_OFF_COLOR = (0, 0, 0)

USE_GPIO = False
TTL_PIN_BCM = 23
TTL_PULSE_SEC = 0.005

EVENT_FIELDS = [
    "utc_iso",
    "event_type",
    "trial_index",
    "repeat_number",
    "image_index",
    "image_id",
    "image_filename",
    "raw_path",
    "planned_duration_sec",
    "start_time_unix",
    "mean_interframe_us",
    "stddev_interframe_us",
    "notes",
]


def sanitize_text(text):
    text = text.strip()
    cleaned = []
    for char in text:
        if char.isalnum() or char in "-_":
            cleaned.append(char)
        else:
            cleaned.append("_")
    return "".join(cleaned)


def utc_iso_now():
    return datetime.now(timezone.utc).isoformat()


def utc_session_stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_dir(path):
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_image_dir():
    for candidate in IMAGE_DIR_CANDIDATES:
        if candidate.exists():
            return candidate
    checked = chr(10).join("  - %s" % candidate for candidate in IMAGE_DIR_CANDIDATES)
    raise RuntimeError(
        "Could not find the Stringer PNG folder. Checked:" + chr(10) + checked + chr(10) +
        "Place stringer_natimg2800_center_crop_png somewhere predictable and update IMAGE_DIR_CANDIDATES if needed."
    )

def list_png_files(image_dir):
    files = sorted(p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() == ".png")
    if not files:
        raise RuntimeError("No PNG files found in %s" % image_dir)
    return files


def parse_image_id_from_filename(path):
    for part in reversed(path.stem.split("_")):
        if part.isdigit():
            return int(part)
    return None


def select_image_subset(all_image_files):
    if N_IMAGES_TO_USE is None:
        return list(all_image_files)
    if N_IMAGES_TO_USE > len(all_image_files):
        raise RuntimeError(
            "N_IMAGES_TO_USE=%d, but only %d PNGs were found." % (N_IMAGES_TO_USE, len(all_image_files))
        )
    rng = random.Random(IMAGE_SUBSET_SEED)
    return sorted(rng.sample(list(all_image_files), N_IMAGES_TO_USE))


def make_trial_sequence(selected_image_files):
    if TRIAL_ORDER_SEED is None:
        seed = int(time.time_ns() % (2 ** 32))
    else:
        seed = int(TRIAL_ORDER_SEED)

    rng = random.Random(seed)
    base_trials = []
    for repeat_idx in range(N_REPEATS):
        for selected_index, path in enumerate(selected_image_files):
            image_id = parse_image_id_from_filename(path)
            base_trials.append(
                {
                    "image_index": selected_index,
                    "image_id": image_id if image_id is not None else selected_index,
                    "image_filename": path.name,
                    "image_path": str(path),
                    "repeat_number": repeat_idx + 1,
                }
            )

    if AVOID_ADJACENT_REPEATS:
        for _ in range(1000):
            rng.shuffle(base_trials)
            if all(base_trials[i]["image_id"] != base_trials[i - 1]["image_id"] for i in range(1, len(base_trials))):
                break
        else:
            print("Warning: could not fully avoid adjacent repeated images.")
    else:
        rng.shuffle(base_trials)

    trials = []
    for trial_index, trial in enumerate(base_trials):
        row = dict(trial)
        row["trial_index"] = trial_index
        row["planned_stim_duration_sec"] = STIM_DURATION_SEC
        row["planned_iti_duration_sec"] = ITI_DURATION_SEC
        trials.append(row)
    return trials, seed


def write_csv(path, rows, fieldnames):
    ensure_dir(path.parent)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def append_csv_row(path, row, fieldnames):
    ensure_dir(path.parent)
    file_exists = path.exists()
    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerow({name: row.get(name, "") for name in fieldnames})


def refreshes_for_seconds(seconds):
    return max(1, int(round(seconds * REFRESH_RATE_HZ)))


def patch_rect(screen_size):
    width, _ = screen_size
    left = width - PHOTODIODE_SIZE_PX - PHOTODIODE_MARGIN_PX
    top = PHOTODIODE_MARGIN_PX
    return left, top, left + PHOTODIODE_SIZE_PX, top + PHOTODIODE_SIZE_PX


def build_canvas(image_path, screen_size, photodiode_on):
    canvas = Image.new("RGB", screen_size, (SCREEN_BACKGROUND_GRAY,) * 3)

    if image_path is not None:
        with Image.open(image_path) as img:
            img = img.convert("RGB")
            img = ImageOps.fit(img, screen_size, method=Image.LANCZOS, centering=(0.5, 0.5))
            canvas.paste(img, (0, 0))

    if ENABLE_PHOTODIODE_PATCH:
        draw = ImageDraw.Draw(canvas)
        fill = PHOTODIODE_ON_COLOR if photodiode_on else PHOTODIODE_OFF_COLOR
        draw.rectangle(patch_rect(screen_size), fill=fill)

    return canvas


def convert_canvas_to_rpg_raw(rpg_module, canvas, raw_path, duration_sec):
    ensure_dir(raw_path.parent)
    fd, source_name = tempfile.mkstemp(prefix="%s_" % raw_path.stem, suffix=".rgb.raw", dir=str(raw_path.parent))
    source_path = Path(source_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canvas.convert("RGB").tobytes())
        rpg_module.convert_raw(
            str(source_path),
            str(raw_path),
            1,
            SCREEN_RESOLUTION[0],
            SCREEN_RESOLUTION[1],
            refreshes_for_seconds(duration_sec),
            SCREEN_COLORMODE,
        )
    finally:
        if source_path.exists():
            try:
                source_path.unlink()
            except OSError:
                pass
    return raw_path


def build_session_raw_cache(rpg_module, session_raw_dir, selected_image_files):
    stim_dir = ensure_dir(session_raw_dir / "stim_on")
    iti_dir = ensure_dir(session_raw_dir / "iti")

    stim_raw_paths = {}
    for image_path in selected_image_files:
        stim_canvas = build_canvas(image_path, SCREEN_RESOLUTION, photodiode_on=True)
        raw_path = stim_dir / (image_path.stem + ".raw")
        convert_canvas_to_rpg_raw(rpg_module, stim_canvas, raw_path, STIM_DURATION_SEC)
        stim_raw_paths[image_path.stem] = raw_path

    iti_canvas = build_canvas(None, SCREEN_RESOLUTION, photodiode_on=False)
    iti_raw_path = iti_dir / "gray_iti.raw"
    convert_canvas_to_rpg_raw(rpg_module, iti_canvas, iti_raw_path, ITI_DURATION_SEC)
    return stim_raw_paths, iti_raw_path


def setup_gpio():
    if not USE_GPIO:
        return None
    try:
        import RPi.GPIO as GPIO
    except ImportError as exc:
        raise RuntimeError("USE_GPIO=True, but RPi.GPIO could not be imported.") from exc

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


def print_environment():
    print("Display environment:")
    print("  DISPLAY=%s" % os.environ.get("DISPLAY"))
    print("  SDL_VIDEODRIVER=%s" % os.environ.get("SDL_VIDEODRIVER"))
    print("  XAUTHORITY=%s" % os.environ.get("XAUTHORITY"))


def prompt_text(prompt):
    try:
        return input(prompt)
    except EOFError:
        return ""


def main():
    print_environment()
    try:
        import rpg
    except ImportError as exc:
        raise RuntimeError(
            "The rpg package is not installed. Install the SjulsonLab rpg repo on the behavior Pi first."
        ) from exc

    mouse_id_raw = prompt_text("Mouse ID: ")
    mouse_id = sanitize_text(mouse_id_raw) or "mouse"
    session_notes = prompt_text("Session notes, optional: ").strip()
    session_stamp = utc_session_stamp()
    session_id = "%s_%s" % (mouse_id, session_stamp)

    image_dir = resolve_image_dir()
    all_pngs = list_png_files(image_dir)
    selected_pngs = select_image_subset(all_pngs)
    trials, sequence_seed = make_trial_sequence(selected_pngs)

    session_root = ensure_dir(OUTPUT_ROOT / mouse_id / session_id)
    raw_cache_root = ensure_dir(session_root / "raw_cache")
    event_log_path = session_root / (session_id + "_event_log.csv")
    selected_images_path = session_root / (session_id + "_selected_images.csv")
    planned_sequence_path = session_root / (session_id + "_planned_sequence.csv")
    metadata_path = session_root / (session_id + "_metadata.json")

    print("Mouse ID: %s" % (mouse_id_raw.strip() or mouse_id))
    print("Session notes, optional: %s" % session_notes)
    print("Session ID: %s" % session_id)
    print("Resolved image directory: %s" % image_dir)
    print("Images used: %d" % len(selected_pngs))
    print("Repeats per image: %d" % N_REPEATS)
    print("Total trials: %d" % len(trials))
    print("Selected images: %s" % selected_images_path)
    print("Planned sequence: %s" % planned_sequence_path)
    print("Event log: %s" % event_log_path)
    print("Metadata: %s" % metadata_path)

    selected_rows = []
    for index, image_path in enumerate(selected_pngs):
        image_id = parse_image_id_from_filename(image_path)
        selected_rows.append(
            {
                "selected_index": index,
                "image_id": image_id if image_id is not None else index,
                "image_filename": image_path.name,
                "image_path": str(image_path),
            }
        )
    write_csv(selected_images_path, selected_rows, ["selected_index", "image_id", "image_filename", "image_path"])

    write_csv(
        planned_sequence_path,
        [
            {
                "trial_index": trial["trial_index"],
                "image_index": trial["image_index"],
                "image_id": trial["image_id"],
                "image_filename": trial["image_filename"],
                "image_path": trial["image_path"],
                "repeat_number": trial["repeat_number"],
                "planned_stim_duration_sec": trial["planned_stim_duration_sec"],
                "planned_iti_duration_sec": trial["planned_iti_duration_sec"],
            }
            for trial in trials
        ],
        [
            "trial_index",
            "image_index",
            "image_id",
            "image_filename",
            "image_path",
            "repeat_number",
            "planned_stim_duration_sec",
            "planned_iti_duration_sec",
        ],
    )

    metadata = {
        "session_id": session_id,
        "utc_iso_start": utc_iso_now(),
        "mouse_id_input": mouse_id_raw,
        "mouse_id": mouse_id,
        "session_notes": session_notes,
        "image_dir": str(image_dir),
        "output_root": str(OUTPUT_ROOT),
        "screen_resolution": list(SCREEN_RESOLUTION),
        "screen_background_gray": SCREEN_BACKGROUND_GRAY,
        "screen_colormode": SCREEN_COLORMODE,
        "refresh_rate_hz": REFRESH_RATE_HZ,
        "n_images_to_use": N_IMAGES_TO_USE,
        "n_repeats": N_REPEATS,
        "image_subset_seed": IMAGE_SUBSET_SEED,
        "trial_order_seed": TRIAL_ORDER_SEED,
        "resolved_trial_order_seed": sequence_seed,
        "avoid_adjacent_repeats": AVOID_ADJACENT_REPEATS,
        "stim_duration_sec": STIM_DURATION_SEC,
        "iti_duration_sec": ITI_DURATION_SEC,
        "initial_gray_sec": INITIAL_GRAY_SEC,
        "final_gray_sec": FINAL_GRAY_SEC,
        "enable_photodiode_patch": ENABLE_PHOTODIODE_PATCH,
        "photodiode_size_px": PHOTODIODE_SIZE_PX,
        "photodiode_margin_px": PHOTODIODE_MARGIN_PX,
        "use_gpio": USE_GPIO,
        "ttl_pin_bcm": TTL_PIN_BCM,
        "selected_images": selected_rows,
        "trials": trials,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + chr(10))

    print("Preparing session raw files...")
    stim_raw_paths, iti_raw_path = build_session_raw_cache(rpg, raw_cache_root, selected_pngs)
    print("Session raw files ready. Starting visual stimulus playback...")
    sys.stdout.flush()
    loaded_stim_raws = {}

    gpio = None
    session_completed = False
    if USE_GPIO:
        gpio = setup_gpio()

    try:
        with rpg.Screen(SCREEN_RESOLUTION, background=SCREEN_BACKGROUND_GRAY, colormode=SCREEN_COLORMODE) as screen:
            screen.display_greyscale(SCREEN_BACKGROUND_GRAY)
            time.sleep(INITIAL_GRAY_SEC)

            for image_path in selected_pngs:
                loaded_stim_raws[image_path.stem] = screen.load_raw(str(stim_raw_paths[image_path.stem]))
            iti_raw = screen.load_raw(str(iti_raw_path))

            append_csv_row(
                event_log_path,
                {
                    "utc_iso": utc_iso_now(),
                    "event_type": "session_start",
                    "notes": "screen_opened",
                },
                EVENT_FIELDS,
            )
            print("Stimulus playback is now active.")
            sys.stdout.flush()

            for trial in trials:
                stem = Path(trial["image_path"]).stem
                if USE_GPIO:
                    ttl_pulse(gpio)

                stim_perf = screen.display_raw(loaded_stim_raws[stem])
                append_csv_row(
                    event_log_path,
                    {
                        "utc_iso": utc_iso_now(),
                        "event_type": "stim_on",
                        "trial_index": trial["trial_index"],
                        "repeat_number": trial["repeat_number"],
                        "image_index": trial["image_index"],
                        "image_id": trial["image_id"],
                        "image_filename": trial["image_filename"],
                        "raw_path": str(stim_raw_paths[stem]),
                        "planned_duration_sec": STIM_DURATION_SEC,
                        "start_time_unix": getattr(stim_perf, "start_time", ""),
                        "mean_interframe_us": getattr(stim_perf, "mean_interframe", ""),
                        "stddev_interframe_us": getattr(stim_perf, "stddev_interframe", ""),
                        "notes": "",
                    },
                    EVENT_FIELDS,
                )

                iti_perf = screen.display_raw(iti_raw)
                append_csv_row(
                    event_log_path,
                    {
                        "utc_iso": utc_iso_now(),
                        "event_type": "iti_on",
                        "trial_index": trial["trial_index"],
                        "repeat_number": trial["repeat_number"],
                        "image_index": trial["image_index"],
                        "image_id": trial["image_id"],
                        "image_filename": trial["image_filename"],
                        "raw_path": str(iti_raw_path),
                        "planned_duration_sec": ITI_DURATION_SEC,
                        "start_time_unix": getattr(iti_perf, "start_time", ""),
                        "mean_interframe_us": getattr(iti_perf, "mean_interframe", ""),
                        "stddev_interframe_us": getattr(iti_perf, "stddev_interframe", ""),
                        "notes": "",
                    },
                    EVENT_FIELDS,
                )

            screen.display_greyscale(SCREEN_BACKGROUND_GRAY)
            time.sleep(FINAL_GRAY_SEC)
            append_csv_row(
                event_log_path,
                {
                    "utc_iso": utc_iso_now(),
                    "event_type": "session_end",
                    "notes": "completed",
                },
                EVENT_FIELDS,
            )
            session_completed = True

    except KeyboardInterrupt:
        append_csv_row(
            event_log_path,
            {
                "utc_iso": utc_iso_now(),
                "event_type": "session_end",
                "notes": "keyboard_interrupt",
            },
            EVENT_FIELDS,
        )
        raise
    finally:
        if gpio is not None:
            try:
                import RPi.GPIO as GPIO
                GPIO.output(TTL_PIN_BCM, GPIO.LOW)
                GPIO.cleanup()
            except Exception:
                pass
        metadata["utc_iso_end"] = utc_iso_now()
        metadata["event_log"] = str(event_log_path)
        metadata["selected_images_csv"] = str(selected_images_path)
        metadata["planned_sequence_csv"] = str(planned_sequence_path)
        metadata["raw_cache_root"] = str(raw_cache_root)
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + chr(10))
        if session_completed:
            print("Session finished. Files are in: %s" % session_root)
        else:
            print("Session stopped early. Partial files are in: %s" % session_root)
        sys.stdout.flush()

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        raise
