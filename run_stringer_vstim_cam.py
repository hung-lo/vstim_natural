#!/usr/bin/env python3
"""
run_stringer_vstim_cam.py

Camera-enabled wrapper around run_stringer_vstim.py.
This keeps the original stimulus runner untouched while adding explicit
remote camera start/stop control for the second Pi.
"""

import json
import shlex
import subprocess
import sys
import time
import threading
from pathlib import Path

import run_stringer_vstim as base

PROJECT_ROOT = base.PROJECT_ROOT
OUTPUT_ROOT = base.OUTPUT_ROOT
CAMERA_CONTROL_SCRIPT = PROJECT_ROOT / "remote_camera_control.py"


def _log_completed_process(proc, label):
    stdout, stderr = proc.communicate()
    if stdout:
        print(stdout, end="")
    if stderr:
        print(stderr, end="", file=sys.stderr)
    if proc.returncode not in (0, None):
        print("%s exited with code %s" % (label, proc.returncode), file=sys.stderr)


def run_camera_control(args, background=False):
    cmd = [sys.executable, str(CAMERA_CONTROL_SCRIPT)] + list(args)
    print("+ " + " ".join(shlex.quote(x) for x in cmd))
    if background:
        proc = subprocess.Popen(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        thread = threading.Thread(target=_log_completed_process, args=(proc, "camera control"), daemon=True)
        thread.start()
        return proc

    try:
        result = subprocess.run(cmd, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise

    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result


def start_camera_recording(mouse_id, session_id):
    print("Starting remote camera recording...")
    return run_camera_control(["start", "--mouse-id", mouse_id, "--session-id", session_id], background=True)


def stop_camera_recording():
    print("Stopping remote camera recording...")
    run_camera_control(["stop"])


def main():
    base.print_environment()
    try:
        import rpg
    except ImportError as exc:
        raise RuntimeError(
            "The rpg package is not installed. Install the SjulsonLab rpg repo on the behavior Pi first."
        ) from exc

    mouse_id_raw = base.prompt_text("Mouse ID: ")
    mouse_id = base.sanitize_text(mouse_id_raw) or "mouse"
    session_notes = base.prompt_text("Session notes, optional: ").strip()
    n_images_to_use = base.prompt_int_or_default("Number of unique images to use", base.N_IMAGES_TO_USE)
    n_repeats = base.prompt_int_or_default("Repeats per image", base.N_REPEATS)

    image_dir = base.resolve_image_dir()
    all_pngs = base.list_png_files(image_dir)
    selected_pngs = base.select_image_subset(all_pngs, n_images_to_use)
    trials, sequence_seed = base.make_trial_sequence(selected_pngs, n_repeats)
    estimated_playback_sec = base.estimate_playback_seconds(len(trials))

    print()
    print("Session setup summary:")
    print("  Mouse ID: %s" % (mouse_id_raw.strip() or mouse_id))
    print("  Session notes, optional: %s" % session_notes)
    print("  Number of unique images: %d" % len(selected_pngs))
    print("  Repeats per image: %d" % n_repeats)
    print("  Total trials: %d" % len(trials))
    print("  Estimated playback time: %s" % base.format_seconds(estimated_playback_sec))
    print("  Output folder: %s" % (OUTPUT_ROOT / mouse_id))

    if not base.prompt_yes_no("Start this session", default_yes=True):
        print("Session aborted before starting. No files were changed.")
        return 0

    session_stamp = base.utc_session_stamp()
    session_id = "%s_%s" % (mouse_id, session_stamp)

    session_root = base.ensure_dir(OUTPUT_ROOT / mouse_id / session_id)
    raw_cache_root = base.ensure_dir(session_root / "raw_cache")
    event_log_path = session_root / (session_id + "_event_log.csv")
    selected_images_path = session_root / (session_id + "_selected_images.csv")
    planned_sequence_path = session_root / (session_id + "_planned_sequence.csv")
    metadata_path = session_root / (session_id + "_metadata.json")

    print("Session ID: %s" % session_id)
    print("Selected images: %s" % selected_images_path)
    print("Planned sequence: %s" % planned_sequence_path)
    print("Event log: %s" % event_log_path)
    print("Metadata: %s" % metadata_path)

    selected_rows = []
    for index, image_path in enumerate(selected_pngs):
        image_id = base.parse_image_id_from_filename(image_path)
        selected_rows.append(
            {
                "selected_index": index,
                "image_id": image_id if image_id is not None else index,
                "image_filename": image_path.name,
                "image_path": str(image_path),
            }
        )
    base.write_csv(selected_images_path, selected_rows, ["selected_index", "image_id", "image_filename", "image_path"])

    base.write_csv(
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
        "utc_iso_start": base.utc_iso_now(),
        "mouse_id_input": mouse_id_raw,
        "mouse_id": mouse_id,
        "session_notes": session_notes,
        "image_dir": str(image_dir),
        "output_root": str(OUTPUT_ROOT),
        "screen_resolution": list(base.SCREEN_RESOLUTION),
        "screen_background_gray": base.SCREEN_BACKGROUND_GRAY,
        "screen_colormode": base.SCREEN_COLORMODE,
        "refresh_rate_hz": base.REFRESH_RATE_HZ,
        "n_images_to_use": n_images_to_use,
        "n_repeats": n_repeats,
        "image_subset_seed": base.IMAGE_SUBSET_SEED,
        "trial_order_seed": base.TRIAL_ORDER_SEED,
        "resolved_trial_order_seed": sequence_seed,
        "avoid_adjacent_repeats": base.AVOID_ADJACENT_REPEATS,
        "stim_duration_sec": base.STIM_DURATION_SEC,
        "iti_duration_sec": base.ITI_DURATION_SEC,
        "initial_gray_sec": base.INITIAL_GRAY_SEC,
        "final_gray_sec": base.FINAL_GRAY_SEC,
        "enable_photodiode_patch": base.ENABLE_PHOTODIODE_PATCH,
        "photodiode_size_px": base.PHOTODIODE_SIZE_PX,
        "photodiode_margin_px": base.PHOTODIODE_MARGIN_PX,
        "use_gpio": base.USE_GPIO,
        "ttl_pin_bcm": base.TTL_PIN_BCM,
        "selected_images": selected_rows,
        "trials": trials,
        "camera_enabled": True,
        "camera_control_script": str(CAMERA_CONTROL_SCRIPT),
        "camera_stop_prompt": "Type y and Enter to stop the camera after the session.",
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + chr(10))

    camera_started = False
    session_completed = False

    try:
        start_camera_recording(mouse_id, session_id)
        camera_started = True
        time.sleep(1.0)

        print("Preparing session raw files...")
        stim_raw_paths, iti_raw_path = base.build_session_raw_cache(rpg, raw_cache_root, selected_pngs)
        print("Session raw files ready. Starting visual stimulus playback...")
        sys.stdout.flush()
        loaded_stim_raws = {}

        gpio = None
        if base.USE_GPIO:
            gpio = base.setup_gpio()

        try:
            with rpg.Screen(base.SCREEN_RESOLUTION, background=base.SCREEN_BACKGROUND_GRAY, colormode=base.SCREEN_COLORMODE) as screen:
                screen.display_greyscale(base.SCREEN_BACKGROUND_GRAY)
                time.sleep(base.INITIAL_GRAY_SEC)

                for image_path in selected_pngs:
                    loaded_stim_raws[image_path.stem] = screen.load_raw(str(stim_raw_paths[image_path.stem]))
                iti_raw = screen.load_raw(str(iti_raw_path))

                base.append_csv_row(
                    event_log_path,
                    {
                        "utc_iso": base.utc_iso_now(),
                        "event_type": "session_start",
                        "notes": "screen_opened",
                    },
                    base.EVENT_FIELDS,
                )
                print("Stimulus playback is now active.")
                sys.stdout.flush()

                playback_start = time.perf_counter()
                for trial_index, trial in enumerate(trials, start=1):
                    stem = Path(trial["image_path"]).stem
                    if base.USE_GPIO:
                        base.ttl_pulse(gpio)

                    stim_perf = screen.display_raw(loaded_stim_raws[stem])
                    base.print_progress(trial_index, len(trials), playback_start)
                    base.append_csv_row(
                        event_log_path,
                        {
                            "utc_iso": base.utc_iso_now(),
                            "event_type": "stim_on",
                            "trial_index": trial["trial_index"],
                            "repeat_number": trial["repeat_number"],
                            "image_index": trial["image_index"],
                            "image_id": trial["image_id"],
                            "image_filename": trial["image_filename"],
                            "raw_path": str(stim_raw_paths[stem]),
                            "planned_duration_sec": base.STIM_DURATION_SEC,
                            "start_time_unix": getattr(stim_perf, "start_time", ""),
                            "mean_interframe_us": getattr(stim_perf, "mean_interframe", ""),
                            "stddev_interframe_us": getattr(stim_perf, "stddev_interframe", ""),
                            "notes": "",
                        },
                        base.EVENT_FIELDS,
                    )

                    iti_perf = screen.display_raw(iti_raw)
                    base.append_csv_row(
                        event_log_path,
                        {
                            "utc_iso": base.utc_iso_now(),
                            "event_type": "iti_on",
                            "trial_index": trial["trial_index"],
                            "repeat_number": trial["repeat_number"],
                            "image_index": trial["image_index"],
                            "image_id": trial["image_id"],
                            "image_filename": trial["image_filename"],
                            "raw_path": str(iti_raw_path),
                            "planned_duration_sec": base.ITI_DURATION_SEC,
                            "start_time_unix": getattr(iti_perf, "start_time", ""),
                            "mean_interframe_us": getattr(iti_perf, "mean_interframe", ""),
                            "stddev_interframe_us": getattr(iti_perf, "stddev_interframe", ""),
                            "notes": "",
                        },
                        base.EVENT_FIELDS,
                    )

                screen.display_greyscale(base.SCREEN_BACKGROUND_GRAY)
                time.sleep(base.FINAL_GRAY_SEC)
                sys.stdout.write("\n")
                sys.stdout.flush()
                base.append_csv_row(
                    event_log_path,
                    {
                        "utc_iso": base.utc_iso_now(),
                        "event_type": "session_end",
                        "notes": "completed",
                    },
                    base.EVENT_FIELDS,
                )
                session_completed = True

        except KeyboardInterrupt:
            base.append_csv_row(
                event_log_path,
                {
                    "utc_iso": base.utc_iso_now(),
                    "event_type": "session_end",
                    "notes": "keyboard_interrupt",
                },
                base.EVENT_FIELDS,
            )
            raise
        finally:
            if base.USE_GPIO and gpio is not None:
                try:
                    import RPi.GPIO as GPIO

                    GPIO.output(base.TTL_PIN_BCM, GPIO.LOW)
                    GPIO.cleanup()
                except Exception:
                    pass

        return 0

    finally:
        camera_stopped = False
        if camera_started:
            if base.prompt_yes_no("Stop camera recording now", default_yes=False):
                try:
                    stop_camera_recording()
                    camera_stopped = True
                except Exception as exc:
                    print("ERROR stopping camera: %s" % exc, file=sys.stderr)
            else:
                print(
                    "Camera left running. Stop it later with: "
                    "python3 remote_camera_control.py stop"
                )

        metadata["utc_iso_end"] = base.utc_iso_now()
        metadata["event_log"] = str(event_log_path)
        metadata["selected_images_csv"] = str(selected_images_path)
        metadata["planned_sequence_csv"] = str(planned_sequence_path)
        metadata["raw_cache_root"] = str(raw_cache_root)
        metadata["camera_started"] = camera_started
        metadata["camera_stopped"] = camera_stopped
        metadata["camera_session_id"] = session_id if camera_started else ""
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + chr(10))
        if session_completed:
            print("Session finished. Files are in: %s" % session_root)
        else:
            print("Session stopped early. Partial files are in: %s" % session_root)
        sys.stdout.flush()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        raise
