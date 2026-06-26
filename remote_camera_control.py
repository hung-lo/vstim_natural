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
    python3 remote_camera_control.py status
    python3 remote_camera_control.py stop-fetch

You can still override the host manually with:
    --camera-host pi@OTHER_IP
"""

import argparse
import csv
import json
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

REMOTE_VIDEO_ROOT = "/home/pi/stim_logs"
LOCAL_VIDEO_ROOT = "stim_logs"

CAMERA_FRAMERATE = 30

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = PROJECT_ROOT / "stim_logs" / ".last_remote_camera_session.json"


def utc_iso_now():
    return datetime.now(timezone.utc).isoformat()


def utc_label():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def sanitize_id(text):
    text = str(text).strip()
    keep = []
    for char in text:
        if char.isalnum() or char in ["-", "_"]:
            keep.append(char)
        else:
            keep.append("_")
    return "".join(keep)


def run_cmd(cmd, check=True, dry_run=False):
    print("+ " + " ".join(shlex.quote(x) for x in cmd))
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0)
    return subprocess.run(cmd, check=check, text=True)


def run_ssh(camera_host, remote_cmd, check=True, dry_run=False):
    return run_cmd(["ssh", camera_host, remote_cmd], check=check, dry_run=dry_run)


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
    if not STATE_FILE.exists():
        raise RuntimeError(
            "No saved camera session state found at %s.\n"
            "Run `python3 remote_camera_control.py start --mouse-id <mouse_id>` first, "
            "or pass `--mouse-id` and `--session-id` to fetch/stop-fetch."
            % STATE_FILE
        )
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


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

    session_id = sanitize_id(args.session_id) if args.session_id else "%s_%s" % (mouse_id, utc_label())

    local_session_dir = (PROJECT_ROOT / LOCAL_VIDEO_ROOT / mouse_id / session_id).resolve()
    local_video_dir = local_session_dir / "video"

    remote_session_dir = "%s/%s/%s" % (REMOTE_VIDEO_ROOT, mouse_id, session_id)
    remote_video_dir = "%s/video" % remote_session_dir
    remote_base_path = "%s/%s" % (remote_video_dir, session_id)

    return {
        "mouse_id": mouse_id,
        "session_id": session_id,
        "local_session_dir": str(local_session_dir),
        "local_video_dir": str(local_video_dir),
        "remote_session_dir": remote_session_dir,
        "remote_video_dir": remote_video_dir,
        "remote_base_path": remote_base_path,
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
        **paths,
    }

    append_event(local_video_dir, "camera_start_requested", state)

    remote_log = "%s/camera_acquisition.log" % paths["remote_video_dir"]

    remote_cmd = (
        "mkdir -p %s && "
        "pkill -f %s || true; "
        "cd %s && "
        "nohup python3 %s %s %d >> %s 2>&1 &"
        % (
            shlex.quote(paths["remote_video_dir"]),
            shlex.quote("video_acquisition/start_acquisition.py"),
            shlex.quote(args.remote_camera_repo),
            shlex.quote(args.remote_camera_start),
            shlex.quote(paths["remote_base_path"]),
            int(args.framerate),
            shlex.quote(remote_log),
        )
    )

    run_ssh(camera_host, remote_cmd, dry_run=args.dry_run)

    append_event(local_video_dir, "camera_start_returned", state)
    save_state(state)

    print("Camera start command sent.")
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
    local_video_dir = Path(state.get("local_video_dir", PROJECT_ROOT / LOCAL_VIDEO_ROOT / "unknown" / "video"))
    local_video_dir.mkdir(parents=True, exist_ok=True)

    append_event(local_video_dir, "camera_stop_requested", {"camera_host": camera_host})

    remote_stop = getattr(args, "remote_camera_stop", None) or state.get("remote_camera_stop") or REMOTE_CAMERA_STOP
    run_ssh(camera_host, "bash %s" % shlex.quote(remote_stop), check=not args.ignore_stop_errors, dry_run=args.dry_run)

    append_event(local_video_dir, "camera_stop_returned", {"camera_host": camera_host})
    print("Camera stop command sent.")
    return state


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

    print("Fetched camera files to: %s" % local_video_dir)
    return state


def status_camera(args):
    state = load_state() if STATE_FILE.exists() else None
    camera_host = resolve_camera_host(args, state)
    remote_cmd = (
        "echo '--- camera acquisition processes ---'; "
        "pgrep -af 'start_acquisition.py' || true; "
        "echo '--- recent camera logs ---'; "
        "find /home/pi/stim_logs -name 'camera_acquisition.log' -type f 2>/dev/null | tail -n 5 || true"
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
