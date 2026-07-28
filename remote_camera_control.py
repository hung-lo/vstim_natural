#!/usr/bin/env python3
"""
remote_camera_control.py

Standalone helper for controlling the second Raspberry Pi camera without
touching run_stringer_vstim.py.

Camera Pi is hard-coded here:
    pi@192.168.1.152

Typical use on the behavior Pi:

    cd /home/pi/vstim_natural
    source .venv/bin/activate

    python3 remote_camera_control.py start --mouse-id testmouse
    python3 remote_camera_control.py preview
    python3 remote_camera_control.py status
    python3 remote_camera_control.py stop-fetch

You can still override the host manually with:
    --camera-host pi@OTHER_IP
"""

import argparse
import csv
import json
import re
import shutil
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_CAMERA_HOST = "pi@192.168.1.152"

REMOTE_CAMERA_REPO = "/home/pi/RPi4_behavior_boxes"
REMOTE_CAMERA_START = "/home/pi/RPi4_behavior_boxes/video_acquisition/start_acquisition.py"
REMOTE_CAMERA_STOP = "/home/pi/RPi4_behavior_boxes/video_acquisition/stop_acquisition.sh"
REMOTE_CAMERA_PREVIEW_LOG = "/home/pi/stim_logs/camera_preview.log"
REMOTE_CAMERA_PREVIEW_PID_FILE = "/tmp/remote_camera_preview.pid"

REMOTE_VIDEO_ROOT = "/home/pi/stim_logs"
LOCAL_VIDEO_ROOT = Path("/mnt/hd")

SSH_OPTIONS = [
    "-n",
    "-T",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=5",
    "-o", "ServerAliveInterval=2",
    "-o", "ServerAliveCountMax=3",
]
CAMERA_START_LAUNCH_TIMEOUT_SEC = 10.0
CAMERA_START_READY_TIMEOUT_SEC = 8.0
CAMERA_READY_POLL_SEC = 0.25
CAMERA_LOG_TAIL_LINES = 50
CAMERA_STOP_TIMEOUT_SEC = 10.0
CAMERA_PREVIEW_LAUNCH_TIMEOUT_SEC = 10.0
CAMERA_PREVIEW_STOP_TIMEOUT_SEC = 5.0
SESSION_NAME_SUFFIX = "vstim_natural"

CAMERA_FRAMERATE = 30

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = LOCAL_VIDEO_ROOT / ".last_remote_camera_session.json"
LEGACY_STATE_FILE = PROJECT_ROOT / "stim_logs" / ".last_remote_camera_session.json"


def utc_iso_now():
    return datetime.now(timezone.utc).isoformat()


def utc_label():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def make_session_name(mouse_id, session_stamp):
    return "%s_%s_%s" % (mouse_id, session_stamp, SESSION_NAME_SUFFIX)


def sanitize_id(text):
    text = str(text).strip()
    keep = []
    for char in text:
        if char.isalnum() or char in ["-", "_"]:
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep)


def run_cmd(cmd, check=True, dry_run=False, timeout=None):
    print("+ " + " ".join(shlex.quote(x) for x in cmd))
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    try:
        return subprocess.run(cmd, check=check, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        timeout_desc = "unknown" if timeout is None else "%.1f" % timeout
        raise RuntimeError("Command timed out after %s seconds: %s" % (timeout_desc, " ".join(shlex.quote(x) for x in cmd))) from exc
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="")
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise


def run_ssh(camera_host, remote_cmd, check=True, dry_run=False, timeout=None):
    return run_cmd(["ssh", *SSH_OPTIONS, camera_host, remote_cmd], check=check, dry_run=dry_run, timeout=timeout)


def build_remote_camera_launch_command(paths, framerate, remote_camera_repo, remote_camera_start, remote_log, pid_file):
    return (
        "set -e; "
        f"mkdir -p {shlex.quote(paths['remote_video_dir'])}; "
        f"cd {shlex.quote(remote_camera_repo)}; "
        f"nohup python3 {shlex.quote(remote_camera_start)} {shlex.quote(paths['remote_base_path'])} {int(framerate)} </dev/null >> {shlex.quote(remote_log)} 2>&1 & "
        "pid=$!; "
        f"echo \"$pid\" > {shlex.quote(pid_file)}; "
        "printf 'CAMERA_PID=%s\\n' \"$pid\""
    )


def build_remote_pid_check_command(pid_file):
    return (
        f"pid=$(cat {shlex.quote(pid_file)} 2>/dev/null || true); "
        "if [ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null; then "
        "echo \"RUNNING:$pid\"; "
        "exit 0; "
        "fi; "
        "echo NOT_RUNNING; "
        "exit 1"
    )


def build_remote_log_tail_command(log_file, n_lines=CAMERA_LOG_TAIL_LINES):
    return "tail -n %d %s 2>/dev/null || true" % (int(n_lines), shlex.quote(log_file))


def build_remote_camera_stop_command(pid_file, remote_stop_script):
    return (
        "set -e; "
        f"pid=$(cat {shlex.quote(pid_file)} 2>/dev/null || true); "
        "if [ -n \"$pid\" ] && [ -r /proc/$pid/cmdline ]; then "
        "cmdline=$(tr '\\0' ' ' < /proc/$pid/cmdline); "
        "case \"$cmdline\" in "
        "*start_acquisition.py*) "
        "kill \"$pid\" 2>/dev/null || true; "
        "sleep 1; "
        "kill -9 \"$pid\" 2>/dev/null || true; "
        ";; "
        "*) echo 'PID file does not match expected acquisition process; falling back to stop script' >&2; ;; "
        "esac; "
        f"rm -f {shlex.quote(pid_file)}; "
        "fi; "
        f"bash {shlex.quote(remote_stop_script)}"
    )


def parse_camera_pid(stdout):
    match = re.search(r"CAMERA_PID=(\d+)", stdout or "")
    if match:
        return int(match.group(1))
    match = re.search(r"RUNNING:(\d+)", stdout or "")
    if match:
        return int(match.group(1))
    return None


def tail_remote_log(camera_host, log_file, dry_run=False):
    result = run_ssh(
        camera_host,
        build_remote_log_tail_command(log_file),
        check=False,
        dry_run=dry_run,
        timeout=CAMERA_START_READY_TIMEOUT_SEC,
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    return (stdout + stderr).strip()


def wait_for_remote_camera_ready(camera_host, pid_file, log_file, dry_run=False, timeout_sec=CAMERA_START_READY_TIMEOUT_SEC):
    if dry_run:
        return {"camera_pid": None, "stdout": "[dry-run]", "returncode": 0}

    deadline = time.monotonic() + float(timeout_sec)
    last_output = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        poll_timeout = min(CAMERA_READY_POLL_SEC, max(0.2, remaining))
        result = run_ssh(
            camera_host,
            build_remote_pid_check_command(pid_file),
            check=False,
            dry_run=dry_run,
            timeout=poll_timeout + 2.0,
        )
        last_output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            pid = parse_camera_pid(last_output)
            return {"camera_pid": pid, "stdout": last_output, "returncode": result.returncode}
        time.sleep(min(CAMERA_READY_POLL_SEC, max(0.0, remaining)))

    tail = tail_remote_log(camera_host, log_file, dry_run=dry_run)
    raise RuntimeError(
        "Camera did not report ready within %.1f seconds. Last remote log tail:\n%s"
        % (timeout_sec, tail or last_output or "<no output>")
    )


def run_rsync(camera_host, remote_dir, local_dir, dry_run=False):
    local_dir.mkdir(parents=True, exist_ok=True)
    return run_cmd(
        [
            "rsync",
            "-av",
            "--progress",
            "--remove-source-files",
            f"{camera_host}:{remote_dir.rstrip('/')}/",
            str(local_dir) + "/",
        ],
        check=True,
        dry_run=dry_run,
    )


def convert_h264_to_mp4(local_video_dir, framerate=CAMERA_FRAMERATE, dry_run=False):
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("ffmpeg not found; skipping mp4 conversion.")
        return

    h264_files = sorted(local_video_dir.glob("*.h264"))
    if not h264_files:
        print("No .h264 files found for mp4 conversion.")
        return

    for input_path in h264_files:
        output_path = input_path.with_suffix(".mp4")
        if output_path.exists():
            print("MP4 already exists, skipping: %s" % output_path)
            continue

        cmd = [
            ffmpeg,
            "-y",
            "-framerate",
            str(framerate),
            "-i",
            str(input_path),
            "-c:v",
            "copy",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        print("+ " + " ".join(shlex.quote(x) for x in cmd))
        if dry_run:
            continue
        try:
            subprocess.run(cmd, check=True, text=True, capture_output=True)
        except subprocess.CalledProcessError as exc:
            if exc.stdout:
                print(exc.stdout, end="")
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
            raise
        print("Converted %s -> %s" % (input_path.name, output_path.name))


def append_event(local_video_dir, event, details=None):
    local_video_dir.mkdir(parents=True, exist_ok=True)
    path = local_video_dir / "camera_control_events.csv"
    exists = path.exists()

    fieldnames = ["unix_time_utc_sec", "iso_time_utc", "event", "details_json"]
    row = {
        "unix_time_utc_sec": "%.6f" % time.time(),
        "iso_time_utc": utc_iso_now(),
        "event": event,
        "details_json": json.dumps(details or {}, sort_keys=True),
    }

    with path.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    print("Saved state: %s" % STATE_FILE)


def load_state():
    state_file = STATE_FILE if STATE_FILE.exists() else LEGACY_STATE_FILE if LEGACY_STATE_FILE.exists() else None
    if state_file is None:
        raise RuntimeError(
            "No saved camera session state found at %s.\n"
            "Also checked legacy location %s.\n"
            "Run `python3 remote_camera_control.py start --mouse-id <mouse_id>` first, "
            "or pass `--mouse-id` and `--session-id` to fetch/stop-fetch."
            % (STATE_FILE, LEGACY_STATE_FILE)
        )
    state = json.loads(state_file.read_text(encoding="utf-8"))
    if state_file == LEGACY_STATE_FILE and state.get("mouse_id") and state.get("session_id"):
        session_id = state["session_id"]
        local_session_dir = (LOCAL_VIDEO_ROOT / session_id).resolve()
        state["local_session_dir"] = str(local_session_dir)
        state["local_video_dir"] = str(local_session_dir / "video")
    return state


def build_state_from_args(args):
    if not getattr(args, "mouse_id", None):
        raise RuntimeError(
            "No saved camera session state found. Pass `--mouse-id` and `--session-id`, "
            "or run `start` first."
        )
    if not getattr(args, "session_id", None):
        raise RuntimeError(
            "No saved camera session state found. Pass `--session-id` as well, "
            "or run `start` first so the session ID is saved automatically."
        )

    paths = make_session_paths(args)
    camera_host = resolve_camera_host(args)
    return {
        "created_utc": utc_iso_now(),
        "camera_host": camera_host,
        "framerate": getattr(args, "framerate", CAMERA_FRAMERATE),
        "remote_camera_repo": getattr(args, "remote_camera_repo", REMOTE_CAMERA_REPO),
        "remote_camera_start": getattr(args, "remote_camera_start", REMOTE_CAMERA_START),
        "remote_camera_stop": getattr(args, "remote_camera_stop", REMOTE_CAMERA_STOP),
        **paths,
    }


def resolve_camera_host(args, state=None):
    if getattr(args, "camera_host", None):
        return args.camera_host
    if state and state.get("camera_host"):
        return state["camera_host"]
    return DEFAULT_CAMERA_HOST


def make_session_paths(args):
    mouse_id = sanitize_id(args.mouse_id)
    if not mouse_id:
        raise RuntimeError("mouse ID cannot be empty")

    session_id = sanitize_id(args.session_id) if args.session_id else make_session_name(mouse_id, utc_label())

    local_session_dir = (LOCAL_VIDEO_ROOT / session_id).resolve()
    local_video_dir = local_session_dir / "video"

    remote_session_dir = "%s/%s" % (REMOTE_VIDEO_ROOT, session_id)
    remote_video_dir = "%s/video" % remote_session_dir
    remote_base_path = "%s/%s" % (remote_video_dir, session_id)
    remote_log_file = "%s/camera_acquisition.log" % remote_video_dir
    remote_pid_file = "%s/camera_acquisition.pid" % remote_video_dir

    return {
        "mouse_id": mouse_id,
        "session_id": session_id,
        "local_session_dir": str(local_session_dir),
        "local_video_dir": str(local_video_dir),
        "remote_session_dir": remote_session_dir,
        "remote_video_dir": remote_video_dir,
        "remote_base_path": remote_base_path,
        "remote_log_file": remote_log_file,
        "remote_pid_file": remote_pid_file,
    }


def start_camera(args):
    camera_host = resolve_camera_host(args)
    paths = make_session_paths(args)
    local_video_dir = Path(paths["local_video_dir"])
    local_video_dir.mkdir(parents=True, exist_ok=True)

    state = {
        "created_utc": utc_iso_now(),
        "camera_host": camera_host,
        "framerate": args.framerate,
        "remote_camera_repo": args.remote_camera_repo,
        "remote_camera_start": args.remote_camera_start,
        "remote_camera_stop": args.remote_camera_stop,
        "camera_status": "launch_requested",
        "camera_pid": None,
        "camera_start_requested_utc": utc_iso_now(),
        "camera_launch_returned_utc": None,
        "camera_start_confirmed_utc": None,
        **paths,
    }

    append_event(local_video_dir, "camera_start_requested", state)
    save_state(state)

    remote_log = paths["remote_log_file"]
    remote_pid_file = paths["remote_pid_file"]
    launch_cmd = build_remote_camera_launch_command(
        paths,
        args.framerate,
        args.remote_camera_repo,
        args.remote_camera_start,
        remote_log,
        remote_pid_file,
    )

    launch_timed_out = False
    launch_stdout = ""
    try:
        launch_result = run_ssh(
            camera_host,
            launch_cmd,
            dry_run=args.dry_run,
            timeout=CAMERA_START_LAUNCH_TIMEOUT_SEC,
        )
        launch_stdout = launch_result.stdout or ""
        state["camera_status"] = "launch_returned"
        state["camera_launch_returned_utc"] = utc_iso_now()
        state["camera_pid"] = parse_camera_pid(launch_stdout) or state.get("camera_pid")
        save_state(state)
        append_event(
            local_video_dir,
            "camera_start_returned",
            {
                **state,
                "launch_stdout": launch_stdout,
            },
        )
    except RuntimeError as exc:
        if "timed out" in str(exc).lower():
            launch_timed_out = True
            state["camera_launch_timed_out"] = True
            print("Camera launch SSH timed out; checking whether the remote process still started.")
            save_state(state)
        else:
            state["camera_status"] = "start_failed"
            state["camera_start_failed_utc"] = utc_iso_now()
            state["camera_start_error"] = str(exc)
            save_state(state)
            append_event(
                local_video_dir,
                "camera_start_failed",
                {
                    "camera_host": camera_host,
                    "error": str(exc),
                },
            )
            raise

    try:
        ready = wait_for_remote_camera_ready(
            camera_host,
            remote_pid_file,
            remote_log,
            dry_run=args.dry_run,
            timeout_sec=CAMERA_START_READY_TIMEOUT_SEC,
        )
        camera_pid = ready.get("camera_pid")
        if camera_pid is not None:
            state["camera_pid"] = camera_pid
        state["camera_status"] = "recording_confirmed"
        state["camera_start_confirmed_utc"] = utc_iso_now()
        state["camera_launch_timed_out"] = launch_timed_out
        save_state(state)
        append_event(
            local_video_dir,
            "camera_recording_confirmed",
            {
                "camera_host": camera_host,
                "camera_pid": state.get("camera_pid"),
                "camera_pid_confirmed": True,
                "camera_launch_timed_out": launch_timed_out,
                "remote_pid_file": remote_pid_file,
                "remote_log_file": remote_log,
            },
        )
    except Exception as exc:
        tail = tail_remote_log(camera_host, remote_log, dry_run=args.dry_run)
        print("Camera startup failed. Remote log tail\n%s" % (tail or "<no log output>"), file=sys.stderr)
        state["camera_status"] = "start_failed"
        state["camera_start_failed_utc"] = utc_iso_now()
        state["camera_start_error"] = str(exc)
        save_state(state)
        append_event(
            local_video_dir,
            "camera_start_failed",
            {
                "camera_host": camera_host,
                "error": str(exc),
                "remote_log_tail": tail,
            },
        )
        try:
            stop_cmd = build_remote_camera_stop_command(remote_pid_file, args.remote_camera_stop)
            run_ssh(
                camera_host,
                stop_cmd,
                check=False,
                dry_run=args.dry_run,
                timeout=CAMERA_STOP_TIMEOUT_SEC,
            )
        except Exception:
            pass
        raise RuntimeError("Camera recording did not become ready.") from exc

    append_event(local_video_dir, "camera_start_returned", state)
    print("Camera acquisition process confirmed running.")
    print("Camera host:      %s" % camera_host)
    print("Remote video dir: %s" % paths["remote_video_dir"])
    print("Local video dir:  %s" % local_video_dir)
    return state


def stop_camera(args, state=None):
    if state is None:
        try:
            state = load_state()
        except RuntimeError:
            state = {}

    camera_host = resolve_camera_host(args, state)
    local_video_dir = Path(state.get("local_video_dir", LOCAL_VIDEO_ROOT / "unknown" / "video"))
    local_video_dir.mkdir(parents=True, exist_ok=True)
    remote_pid_file = state.get("remote_pid_file") or "%s/camera_acquisition.pid" % state.get("remote_video_dir", REMOTE_VIDEO_ROOT)

    append_event(local_video_dir, "camera_stop_requested", {"camera_host": camera_host, "remote_pid_file": remote_pid_file})
    state["camera_status"] = "stop_requested"
    state["camera_stop_requested_utc"] = utc_iso_now()
    save_state(state)

    remote_stop = getattr(args, "remote_camera_stop", None) or state.get("remote_camera_stop") or REMOTE_CAMERA_STOP
    stop_cmd = build_remote_camera_stop_command(remote_pid_file, remote_stop)
    run_ssh(
        camera_host,
        stop_cmd,
        check=not args.ignore_stop_errors,
        dry_run=args.dry_run,
        timeout=CAMERA_STOP_TIMEOUT_SEC,
    )

    state["camera_status"] = "stopped"
    state["camera_stop_returned_utc"] = utc_iso_now()
    save_state(state)
    append_event(local_video_dir, "camera_stop_returned", {"camera_host": camera_host, "remote_pid_file": remote_pid_file})
    print("Camera stop command sent.")
    return state


def preview_camera(args):
    camera_host = resolve_camera_host(args)
    preview_cmd = (
        "set -e; "
        "cam=$(command -v rpicam-hello || command -v libcamera-hello); "
        "if [ -z \"$cam\" ]; then echo 'No rpicam-hello or libcamera-hello found' >&2; exit 1; fi; "
        "mkdir -p /home/pi/stim_logs; "
        "nohup \"$cam\" -t 0 --fullscreen </dev/null >%s 2>&1 & echo $! > %s"
        % (shlex.quote(REMOTE_CAMERA_PREVIEW_LOG), shlex.quote(REMOTE_CAMERA_PREVIEW_PID_FILE))
    )
    stop_cmd = (
        "if [ -f %s ]; then "
        "pid=$(cat %s); "
        "kill \"$pid\" 2>/dev/null || true; "
        "sleep 0.5; "
        "kill -9 \"$pid\" 2>/dev/null || true; "
        "rm -f %s; "
        "fi"
        % (
            shlex.quote(REMOTE_CAMERA_PREVIEW_PID_FILE),
            shlex.quote(REMOTE_CAMERA_PREVIEW_PID_FILE),
            shlex.quote(REMOTE_CAMERA_PREVIEW_PID_FILE),
        )
    )

    print("Starting remote camera preview on %s..." % camera_host)
    run_ssh(camera_host, preview_cmd, dry_run=args.dry_run, timeout=CAMERA_PREVIEW_LAUNCH_TIMEOUT_SEC)
    print("Preview started. Type y and Enter to stop it.")
    if not args.dry_run:
        while True:
            try:
                response = input("> ").strip().lower()
            except EOFError:
                response = "y"
            if response == "y":
                break
            print("Preview still running. Type y and Enter to stop.")
        run_ssh(camera_host, stop_cmd, dry_run=args.dry_run, timeout=CAMERA_PREVIEW_STOP_TIMEOUT_SEC)
        print("Preview stopped.")
    else:
        print("Dry run finished; preview was not actually started.")


def status_camera(args):
    state = load_state() if STATE_FILE.exists() else None
    camera_host = resolve_camera_host(args, state)
    remote_pid_file = None
    remote_log_file = None
    if state:
        remote_pid_file = state.get("remote_pid_file")
        remote_log_file = state.get("remote_log_file")
    if not remote_pid_file:
        remote_pid_file = "%s/camera_acquisition.pid" % (state.get("remote_video_dir") if state else REMOTE_VIDEO_ROOT)
    if not remote_log_file:
        remote_log_file = "%s/camera_acquisition.log" % (state.get("remote_video_dir") if state else REMOTE_VIDEO_ROOT)
    remote_cmd = (
        "echo '--- saved camera state ---'; "
        "if [ -f %s ]; then cat %s; else echo 'No pid file'; fi; "
        "echo '--- camera acquisition processes ---'; "
        "pid=$(cat %s 2>/dev/null || true); "
        "if [ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null; then echo RUNNING:$pid; else echo NOT_RUNNING; fi; "
        "echo '--- recent camera logs ---'; "
        "tail -n 20 %s 2>/dev/null || true"
        % (
            shlex.quote(remote_pid_file),
            shlex.quote(remote_pid_file),
            shlex.quote(remote_pid_file),
            shlex.quote(remote_log_file),
        )
    )
    run_ssh(camera_host, remote_cmd, dry_run=args.dry_run, timeout=CAMERA_START_READY_TIMEOUT_SEC)


def fetch_camera(args, state=None):
    if state is None:
        try:
            state = load_state()
        except RuntimeError:
            state = build_state_from_args(args)

    camera_host = resolve_camera_host(args, state)
    remote_video_dir = state["remote_video_dir"]
    local_video_dir = Path(state["local_video_dir"])
    local_video_dir.mkdir(parents=True, exist_ok=True)

    append_event(
        local_video_dir,
        "camera_fetch_requested",
        {
            "camera_host": camera_host,
            "remote_video_dir": remote_video_dir,
            "local_video_dir": str(local_video_dir),
        },
    )

    run_rsync(camera_host, remote_video_dir, local_video_dir, dry_run=args.dry_run)

    append_event(
        local_video_dir,
        "camera_fetch_returned",
        {
            "camera_host": camera_host,
            "remote_video_dir": remote_video_dir,
            "local_video_dir": str(local_video_dir),
        },
    )

    append_event(
        local_video_dir,
        "camera_conversion_requested",
        {
            "camera_host": camera_host,
            "local_video_dir": str(local_video_dir),
        },
    )
    convert_h264_to_mp4(local_video_dir, framerate=state.get("framerate", CAMERA_FRAMERATE), dry_run=args.dry_run)
    append_event(
        local_video_dir,
        "camera_conversion_returned",
        {
            "camera_host": camera_host,
            "local_video_dir": str(local_video_dir),
        },
    )

    print("Fetched camera files to: %s" % local_video_dir)
    return state


def status_camera(args):
    state = load_state() if STATE_FILE.exists() else None
    camera_host = resolve_camera_host(args, state)
    safe_start_pattern = "[v]ideo_acquisition/start_acquisition.py"
    remote_cmd = (
        "echo '--- camera acquisition processes ---'; "
        "pgrep -af %s || true; "
        "echo '--- recent camera logs ---'; "
        "find /home/pi/stim_logs -name 'camera_acquisition.log' -type f 2>/dev/null | tail -n 5 || true"
        % shlex.quote(safe_start_pattern)
    )
    run_ssh(camera_host, remote_cmd, dry_run=args.dry_run)


def print_last_state(args):
    print(json.dumps(load_state(), indent=2))


def build_parser():
    parser = argparse.ArgumentParser(description="Standalone second-Pi camera controller for vstim_natural.")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--camera-host",
        default=None,
        help="SSH host for camera Pi. Default: %s" % DEFAULT_CAMERA_HOST,
    )
    common.add_argument("--dry-run", action="store_true", help="Print commands without running them.")

    start = sub.add_parser("start", parents=[common], help="Start remote camera recording.")
    start.add_argument("--mouse-id", required=True, help="Mouse ID for session folder.")
    start.add_argument("--session-id", default=None, help="Optional session ID. Default: mouse_UTCtimestamp.")
    start.add_argument("--framerate", type=int, default=CAMERA_FRAMERATE)
    start.add_argument("--remote-camera-repo", default=REMOTE_CAMERA_REPO)
    start.add_argument("--remote-camera-start", default=REMOTE_CAMERA_START)
    start.add_argument("--remote-camera-stop", default=REMOTE_CAMERA_STOP)
    start.set_defaults(func=start_camera)

    stop = sub.add_parser("stop", parents=[common], help="Stop remote camera recording.")
    stop.add_argument("--remote-camera-stop", default=REMOTE_CAMERA_STOP)
    stop.add_argument("--ignore-stop-errors", action="store_true", default=False)
    stop.set_defaults(func=stop_camera)

    fetch = sub.add_parser("fetch", parents=[common], help="Fetch last remote camera files with rsync.")
    fetch.set_defaults(func=fetch_camera)

    preview = sub.add_parser("preview", parents=[common], help="Start a live camera preview, then stop it when you type y.")
    preview.set_defaults(func=preview_camera)

    stop_fetch = sub.add_parser("stop-fetch", parents=[common], help="Stop recording, then fetch files.")
    stop_fetch.add_argument("--mouse-id", default=None, help="Mouse ID if no saved session state exists yet.")
    stop_fetch.add_argument("--session-id", default=None, help="Session ID if no saved session state exists yet.")
    stop_fetch.add_argument("--remote-camera-stop", default=REMOTE_CAMERA_STOP)
    stop_fetch.add_argument("--ignore-stop-errors", action="store_true", default=False)

    def do_stop_fetch(args):
        try:
            state = load_state()
        except RuntimeError:
            state = build_state_from_args(args)
        stop_camera(args, state)
        time.sleep(2.0)
        fetch_camera(args, state)

    stop_fetch.set_defaults(func=do_stop_fetch)

    status = sub.add_parser("status", parents=[common], help="Check whether camera acquisition is running.")
    status.set_defaults(func=status_camera)

    last = sub.add_parser("last-state", help="Print last camera session state.")
    last.set_defaults(func=print_last_state)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        raise
