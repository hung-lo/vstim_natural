#!/usr/bin/env python3
"""
run_stringer_vstim_cam.py

Camera-enabled wrapper around run_stringer_vstim.py.
This keeps the original stimulus runner untouched while adding explicit
remote camera start/stop control for the second Pi.
"""

import json
import math
import select
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import run_stringer_vstim as base

PROJECT_ROOT = base.PROJECT_ROOT
OUTPUT_ROOT = base.OUTPUT_ROOT
CAMERA_CONTROL_SCRIPT = PROJECT_ROOT / "remote_camera_control.py"
DEFAULT_PRESTIM_BASELINE_MINUTES = 3.0
BASELINE_INPUT_POLL_SEC = 0.25
BASELINE_STATUS_INTERVAL_SEC = 10.0


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
    return run_camera_control(["start", "--mouse-id", mouse_id, "--session-id", session_id], background=False)


def stop_camera_recording():
    print("Stopping remote camera recording...")
    run_camera_control(["stop"])


def fetch_camera_recording():
    print("Fetching camera files to box 151...")
    run_camera_control(["fetch"])




def prompt_float_or_default(prompt, default_value, minimum=0.0):
    raw = base.prompt_text("%s [%g]: " % (prompt, default_value)).strip()
    if not raw:
        return default_value
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError("%s must be a finite number" % prompt)
    if value < minimum:
        raise ValueError("%s must be at least %g" % (prompt, minimum))
    return value


def is_early_start_command(line):
    return line.strip().lower() in {"y", "yes"}


def watch_for_early_start(force_start_event, stop_event):
    try:
        stdin = sys.stdin
        if not hasattr(stdin, "isatty") or not stdin.isatty():
            return
    except Exception:
        return

    while not stop_event.is_set() and not force_start_event.is_set():
        try:
            readable, _, _ = select.select([sys.stdin], [], [], BASELINE_INPUT_POLL_SEC)
        except (OSError, ValueError, AttributeError):
            return

        if stop_event.is_set() or force_start_event.is_set():
            return
        if not readable:
            continue

        try:
            line = sys.stdin.readline()
        except Exception:
            return

        if line == "":
            return

        if is_early_start_command(line):
            print(
                "Early start requested. Stimuli will begin as soon as raw preparation and the initial gray period are complete."
            )
            force_start_event.set()
            return

        stripped = line.strip()
        if stripped:
            print(
                "Ignoring input %r; baseline continues. Type y and Enter to request early stimulus start." % stripped
            )


def start_prestimulus_early_start_monitor():
    force_start_event = threading.Event()
    stop_event = threading.Event()
    thread = None
    interactive = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
    if interactive:
        thread = threading.Thread(target=watch_for_early_start, args=(force_start_event, stop_event), daemon=True)
        thread.start()
    else:
        print("Interactive early-start override is unavailable because stdin is not a TTY.")
    return force_start_event, stop_event, thread, interactive


def stop_prestimulus_early_start_monitor(stop_event, thread):
    if stop_event is not None:
        stop_event.set()
    if thread is not None:
        thread.join(timeout=1.0)


def wait_for_prestimulus_gate(baseline_start_monotonic, requested_baseline_sec, force_start_event):
    initial_remaining_sec = max(0.0, requested_baseline_sec - (time.monotonic() - baseline_start_monotonic))
    result = {
        "requested_sec": requested_baseline_sec,
        "remaining_sec_at_gate_entry": initial_remaining_sec,
        "forced": False,
        "end_reason": "",
    }

    if force_start_event.is_set():
        result["forced"] = True
        result["end_reason"] = "user_override"
    elif initial_remaining_sec <= 0.0:
        result["end_reason"] = "timer_satisfied_during_preparation"
    else:
        remaining_sec = initial_remaining_sec
        while remaining_sec > 0.0 and not force_start_event.is_set():
            wait_sec = min(BASELINE_STATUS_INTERVAL_SEC, remaining_sec)
            if force_start_event.wait(timeout=wait_sec):
                result["forced"] = True
                result["end_reason"] = "user_override"
                break
            remaining_sec = max(0.0, requested_baseline_sec - (time.monotonic() - baseline_start_monotonic))
            if remaining_sec > 0.0:
                print("Pre-stimulus baseline remaining: %s ..." % base.format_seconds(remaining_sec))
        else:
            if result["end_reason"] != "user_override":
                result["end_reason"] = "timer_elapsed"

    result["elapsed_sec"] = time.monotonic() - baseline_start_monotonic
    if not result["end_reason"]:
        result["end_reason"] = "timer_elapsed"
    return result


def prestimulus_result_message(baseline_result):
    reason = baseline_result["end_reason"]
    if reason == "user_override":
        return "Pre-stimulus baseline ended early by user request. Starting visual stimulus playback..."
    if reason == "timer_satisfied_during_preparation":
        return "Requested pre-stimulus baseline was satisfied during preparation. Starting visual stimulus playback..."
    return "Pre-stimulus baseline complete. Starting visual stimulus playback..."




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
    prestim_baseline_minutes = prompt_float_or_default(
        "Pre-stimulus camera baseline in minutes",
        DEFAULT_PRESTIM_BASELINE_MINUTES,
        minimum=0.0,
    )
    prestim_baseline_sec = prestim_baseline_minutes * 60.0

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
    print("  Pre-stimulus camera baseline: %s" % base.format_seconds(prestim_baseline_sec))
    print("  Camera timing: recording starts before RPG raw-file preparation")
    print("  Early start: type y and press Enter after recording begins")
    print("  Post-stimulus camera: no fixed end time; existing stop prompt is retained")
    print("  Total trials: %d" % len(trials))
    print("  Estimated playback time: %s" % base.format_seconds(estimated_playback_sec))
    print("  Output folder root: %s" % OUTPUT_ROOT)
    print("  Session folder name: %s" % base.make_session_name(mouse_id, "YYYYMMDDThhmmssZ"))

    if not base.prompt_yes_no("Start this session", default_yes=True):
        print("Session aborted before starting. No files were changed.")
        return 0

    session_stamp = base.utc_session_stamp()
    session_id = base.make_session_name(mouse_id, session_stamp)

    session_root = base.ensure_dir(OUTPUT_ROOT / session_id)
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
        "prestim_baseline_requested_minutes": prestim_baseline_minutes,
        "prestim_baseline_requested_sec": prestim_baseline_sec,
        "prestim_early_start_enabled": True,
        "prestim_early_start_key": "y",
        "poststim_baseline_mode": "free_end_existing_prompt",
        "selected_images": selected_rows,
        "trials": trials,
        "camera_enabled": True,
        "camera_control_script": str(CAMERA_CONTROL_SCRIPT),
        "camera_stop_prompt": "Type y and Enter to stop the camera after the session.",
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + chr(10))

    camera_started = False
    camera_stopped = False
    session_completed = False
    playback_started = False
    camera_start_returned_utc = ""
    prestim_baseline_start_utc = ""
    prestim_baseline_start_monotonic = None
    baseline_result = None
    raw_cache_build_duration_sec = None
    force_start_event = None
    force_input_stop_event = None
    force_input_thread = None
    monitor_active = False
    gpio = None

    try:
        start_camera_recording(mouse_id, session_id)
        camera_started = True
        camera_start_returned_utc = base.utc_iso_now()
        prestim_baseline_start_utc = camera_start_returned_utc
        prestim_baseline_start_monotonic = time.monotonic()
        base.append_csv_row(
            event_log_path,
            {
                "event_type": "prestim_baseline_start",
                "notes": "requested_sec=%.3f; camera_start_command_returned" % prestim_baseline_sec,
            },
            base.EVENT_FIELDS,
        )

        force_start_event, force_input_stop_event, force_input_thread, monitor_active = start_prestimulus_early_start_monitor()
        print()
        print("Remote camera recording is active.")
        print("Pre-stimulus baseline target: %s." % base.format_seconds(prestim_baseline_sec))
        print("RPG raw files will be prepared while the camera records.")
        print("Type y and press Enter at any time to request early stimulus start.")
        print("The stimulus cannot begin until raw preparation and the initial gray period are complete.")
        sys.stdout.flush()

        print("Preparing session raw files...")
        raw_cache_build_start = time.perf_counter()
        stim_raw_paths, iti_raw_path = base.build_session_raw_cache(rpg, raw_cache_root, selected_pngs)
        raw_cache_build_duration_sec = time.perf_counter() - raw_cache_build_start
        baseline_elapsed_after_raw = time.monotonic() - prestim_baseline_start_monotonic
        baseline_remaining_after_raw = max(0.0, prestim_baseline_sec - baseline_elapsed_after_raw)
        base.append_csv_row(
            event_log_path,
            {
                "event_type": "raw_cache_ready",
                "notes": "build_duration_sec=%.6f; baseline_elapsed_sec=%.6f" % (
                    raw_cache_build_duration_sec,
                    baseline_elapsed_after_raw,
                ),
            },
            base.EVENT_FIELDS,
        )
        print("Session raw files are ready.")
        print(
            "Camera baseline elapsed: %s; remaining: %s."
            % (base.format_seconds(baseline_elapsed_after_raw), base.format_seconds(baseline_remaining_after_raw))
        )
        print("Type y and press Enter to start early.")
        sys.stdout.flush()

        if base.USE_GPIO:
            gpio = base.setup_gpio()

        try:
            with rpg.Screen(base.SCREEN_RESOLUTION, background=base.SCREEN_BACKGROUND_GRAY, colormode=base.SCREEN_COLORMODE) as screen:
                screen.display_greyscale(base.SCREEN_BACKGROUND_GRAY)
                time.sleep(base.INITIAL_GRAY_SEC)

                loaded_stim_raws = {}
                for image_path in selected_pngs:
                    loaded_stim_raws[image_path.stem] = screen.load_raw(str(stim_raw_paths[image_path.stem]))
                iti_raw = screen.load_raw(str(iti_raw_path))

                baseline_result = wait_for_prestimulus_gate(
                    prestim_baseline_start_monotonic,
                    prestim_baseline_sec,
                    force_start_event,
                )
                stop_prestimulus_early_start_monitor(force_input_stop_event, force_input_thread)
                force_input_thread = None
                force_input_stop_event = None

                base.append_csv_row(
                    event_log_path,
                    {
                        "event_type": "prestim_baseline_end",
                        "notes": "reason=%s; requested_sec=%.3f; actual_sec=%.3f; forced=%s"
                        % (
                            baseline_result["end_reason"],
                            baseline_result["requested_sec"],
                            baseline_result["elapsed_sec"],
                            baseline_result["forced"],
                        ),
                    },
                    base.EVENT_FIELDS,
                )
                base.append_csv_row(
                    event_log_path,
                    {
                        "event_type": "session_start",
                        "notes": "screen_opened",
                    },
                    base.EVENT_FIELDS,
                )
                print(prestimulus_result_message(baseline_result))
                print("Stimulus playback is now active.")
                sys.stdout.flush()

                playback_started = True
                base.run_trial_sequence(
                    screen,
                    trials,
                    loaded_stim_raws,
                    iti_raw,
                    stim_raw_paths,
                    iti_raw_path,
                    event_log_path,
                    gpio=gpio if base.USE_GPIO else None,
                )

                screen.display_greyscale(base.SCREEN_BACKGROUND_GRAY)
                time.sleep(base.FINAL_GRAY_SEC)
                sys.stdout.write("\n")
                sys.stdout.flush()
                base.append_csv_row(
                    event_log_path,
                    {
                        "event_type": "session_end",
                        "notes": "completed",
                    },
                    base.EVENT_FIELDS,
                )
                session_completed = True

        except KeyboardInterrupt:
            stop_prestimulus_early_start_monitor(force_input_stop_event, force_input_thread)
            force_input_thread = None
            force_input_stop_event = None
            if not playback_started:
                base.append_csv_row(
                    event_log_path,
                    {
                        "event_type": "session_end",
                        "notes": "keyboard_interrupt_during_prestim",
                    },
                    base.EVENT_FIELDS,
                )
            else:
                base.append_csv_row(
                    event_log_path,
                    {
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
        stop_prestimulus_early_start_monitor(force_input_stop_event, force_input_thread)
        camera_stopped = False
        if camera_started:
            if base.prompt_yes_no("Stop camera recording now", default_yes=False):
                try:
                    stop_camera_recording()
                    time.sleep(2.0)
                    fetch_camera_recording()
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
        metadata["session_completed"] = session_completed
        metadata["playback_started"] = playback_started
        metadata["camera_start_returned_utc"] = camera_start_returned_utc
        metadata["prestim_baseline_start_utc"] = prestim_baseline_start_utc
        metadata["prestim_baseline_actual_sec"] = baseline_result["elapsed_sec"] if baseline_result else ""
        metadata["prestim_baseline_forced"] = baseline_result["forced"] if baseline_result else False
        metadata["prestim_baseline_end_reason"] = baseline_result["end_reason"] if baseline_result else ""
        metadata["prestim_baseline_remaining_sec_at_gate_entry"] = (
            baseline_result["remaining_sec_at_gate_entry"] if baseline_result else ""
        )
        metadata["raw_cache_build_duration_sec"] = raw_cache_build_duration_sec if raw_cache_build_duration_sec is not None else ""
        metadata["prestim_gate_released_utc"] = base.utc_iso_now() if baseline_result else ""
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
