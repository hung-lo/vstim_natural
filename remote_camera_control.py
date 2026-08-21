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
import hashlib
import json
import math
import os
import re
import shutil
import shlex
import subprocess
import sys
import tempfile
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
CAMERA_OUTPUT_READY_TIMEOUT_SEC = 12.0
CAMERA_OUTPUT_READY_POLL_SEC = 0.5
CAMERA_OUTPUT_MIN_BYTES = 1
CAMERA_OUTPUT_GLOB = "*.h264"
CAMERA_RAW_VIDEO_PATTERNS = ("*.h264",)
CAMERA_RSYNC_TIMEOUT_SEC = 300.0
CAMERA_MANIFEST_TIMEOUT_SEC = 300.0
CAMERA_FFMPEG_TIMEOUT_SEC = 600.0
CAMERA_MP4_FASTSTART = False
CAMERA_FFPROBE_TIMEOUT_SEC = 30.0
CAMERA_STOP_TIMEOUT_SEC = 10.0
CAMERA_PREVIEW_LAUNCH_TIMEOUT_SEC = 10.0
CAMERA_STOP_VERIFY_TIMEOUT_SEC = 8.0
CAMERA_STOP_VERIFY_POLL_SEC = 0.25
CAMERA_PREVIEW_STOP_TIMEOUT_SEC = 5.0
SESSION_NAME_SUFFIX = "vstim_natural"
CAMERA_STATE_SCHEMA_VERSION = 1
CONTROLLER_REPOSITORY = "vstim_natural"
PROTOCOL_NAME = "stringer_natural_image_visual_stimulus"

CAMERA_FRAMERATE = 30
JSON_RESULT_PREFIX = "CAMERA_CONTROL_RESULT_JSON="

PROJECT_ROOT = Path(__file__).resolve().parent
STATE_FILE = LOCAL_VIDEO_ROOT / ".vstim_natural_camera_session.json"
GENERIC_STATE_FILE = LOCAL_VIDEO_ROOT / ".last_remote_camera_session.json"
LEGACY_STATE_FILE = PROJECT_ROOT / "stim_logs" / ".last_remote_camera_session.json"
VIDEO_MANIFEST_FILENAME = "video_manifest.json"


class CameraStateError(RuntimeError):
    """Base class for saved-camera-state validation failures."""


class CameraStateNotFoundError(CameraStateError):
    pass


class CameraStateOwnershipError(CameraStateError):
    pass


class CameraStateSchemaError(CameraStateError):
    pass


class CameraStateCorruptError(CameraStateError):
    pass


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


def subprocess_output_to_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def tail_text(text, max_chars=4000):
    text = text or ""
    if len(text) <= max_chars:
        return text
    return "...[truncated]...\n" + text[-max_chars:]


def print_subprocess_output(stdout, stderr):
    stdout_text = subprocess_output_to_text(stdout)
    stderr_text = subprocess_output_to_text(stderr)
    if stdout_text:
        print(stdout_text, end="" if stdout_text.endswith("\n") else "\n")
    if stderr_text:
        print(stderr_text, end="" if stderr_text.endswith("\n") else "\n", file=sys.stderr)
    return stdout_text, stderr_text


def run_cmd(cmd, check=True, dry_run=False, timeout=None):
    print("+ " + " ".join(shlex.quote(x) for x in cmd))
    if dry_run:
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    try:
        return subprocess.run(cmd, check=check, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout_text, stderr_text = print_subprocess_output(exc.stdout, exc.stderr)
        timeout_desc = "unknown" if timeout is None else "%.1f" % timeout
        detail_lines = []
        if stdout_text:
            detail_lines.append("stdout tail:\n%s" % tail_text(stdout_text))
        if stderr_text:
            detail_lines.append("stderr tail:\n%s" % tail_text(stderr_text))
        message = "Command timed out after %s seconds: %s" % (timeout_desc, " ".join(shlex.quote(x) for x in cmd))
        if detail_lines:
            message += "\n" + "\n".join(detail_lines)
        raise RuntimeError(message) from exc
    except subprocess.CalledProcessError as exc:
        print_subprocess_output(exc.stdout, exc.stderr)
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
        "fi; "
        f"bash {shlex.quote(remote_stop_script)}"
    )



def build_remote_camera_stopped_check_command(expected_pid, remote_base_path):
    expected_pid_text = "" if expected_pid is None else str(int(expected_pid))
    return (
        "pid_alive=0; "
        "expected_pid=%s; "
        "if [ -n \"$expected_pid\" ] && kill -0 \"$expected_pid\" 2>/dev/null; then pid_alive=1; fi; "
        "matches=$(pgrep -af '[s]tart_acquisition.py' | grep -F -- %s || true); "
        "if [ \"$pid_alive\" -eq 0 ] && [ -z \"$matches\" ]; then "
        "echo STOPPED; exit 0; "
        "fi; "
        "echo STILL_RUNNING; "
        "if [ -n \"$matches\" ]; then echo \"$matches\"; fi; "
        "exit 1"
        % (shlex.quote(expected_pid_text), shlex.quote(remote_base_path))
    )


def build_remote_output_probe_command(remote_video_dir):
    return (
        "find %s -maxdepth 1 -type f -name %s "
        "-printf '%%p\\t%%s\\n' 2>/dev/null | sort"
        % (shlex.quote(remote_video_dir), shlex.quote(CAMERA_OUTPUT_GLOB))
    )


def parse_camera_pid(stdout):
    match = re.search(r"CAMERA_PID=(\d+)", stdout or "")
    if match:
        return int(match.group(1))
    match = re.search(r"RUNNING:(\d+)", stdout or "")
    if match:
        return int(match.group(1))
    return None


def parse_remote_output_probe(stdout):
    files = {}
    for line in (stdout or "").splitlines():
        try:
            path, size_text = line.rsplit("\t", 1)
            files[path] = int(size_text)
        except (TypeError, ValueError):
            continue
    return files


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


def wait_for_remote_camera_output_growth(
    camera_host,
    pid_file,
    remote_video_dir,
    log_file,
    dry_run=False,
    timeout_sec=CAMERA_OUTPUT_READY_TIMEOUT_SEC,
):
    if dry_run:
        return {
            "camera_output_file": None,
            "initial_size_bytes": 0,
            "confirmed_size_bytes": CAMERA_OUTPUT_MIN_BYTES,
            "camera_output_file_detected": True,
            "camera_output_growing_confirmed": True,
        }

    deadline = time.monotonic() + float(timeout_sec)
    first_sizes = {}
    last_output = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break

        pid_result = run_ssh(
            camera_host,
            build_remote_pid_check_command(pid_file),
            check=False,
            dry_run=dry_run,
            timeout=min(2.0, max(0.2, remaining)) + 2.0,
        )
        if pid_result.returncode != 0:
            tail = tail_remote_log(camera_host, log_file, dry_run=dry_run)
            raise RuntimeError(
                "Camera process stopped while waiting for video output growth. "
                "Last remote log tail:\n%s" % (tail or "<no output>")
            )

        probe_result = run_ssh(
            camera_host,
            build_remote_output_probe_command(remote_video_dir),
            check=False,
            dry_run=dry_run,
            timeout=min(2.0, max(0.2, remaining)) + 2.0,
        )
        last_output = (probe_result.stdout or "") + (probe_result.stderr or "")
        current_sizes = parse_remote_output_probe(probe_result.stdout)
        for path, current_size in current_sizes.items():
            if path not in first_sizes:
                first_sizes[path] = current_size
                continue
            initial_size = first_sizes[path]
            if current_size >= CAMERA_OUTPUT_MIN_BYTES and current_size > initial_size:
                return {
                    "camera_output_file": path,
                    "initial_size_bytes": initial_size,
                    "confirmed_size_bytes": current_size,
                    "camera_output_file_detected": True,
                    "camera_output_growing_confirmed": True,
                }

        time.sleep(min(CAMERA_OUTPUT_READY_POLL_SEC, max(0.0, remaining)))

    tail = tail_remote_log(camera_host, log_file, dry_run=dry_run)
    detected = bool(first_sizes)
    raise RuntimeError(
        "Camera video output %s within %.1f seconds. Last probe output:\n%s\n"
        "Last remote log tail:\n%s"
        % (
            "did not grow" if detected else "was not detected",
            timeout_sec,
            last_output or "<no output>",
            tail or "<no output>",
        )
    )


def wait_for_remote_camera_stopped(
    camera_host,
    expected_pid,
    remote_base_path,
    log_file,
    dry_run=False,
    timeout_sec=CAMERA_STOP_VERIFY_TIMEOUT_SEC,
):
    if dry_run:
        return {"camera_stop_confirmed": True, "stdout": "[dry-run]"}

    deadline = time.monotonic() + float(timeout_sec)
    last_output = ""
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        result = run_ssh(
            camera_host,
            build_remote_camera_stopped_check_command(expected_pid, remote_base_path),
            check=False,
            dry_run=dry_run,
            timeout=min(2.0, max(0.2, remaining)) + 2.0,
        )
        last_output = (result.stdout or "") + (result.stderr or "")
        if result.returncode == 0:
            return {"camera_stop_confirmed": True, "stdout": last_output}
        time.sleep(min(CAMERA_STOP_VERIFY_POLL_SEC, max(0.0, remaining)))

    tail = tail_remote_log(camera_host, log_file, dry_run=dry_run)
    raise RuntimeError(
        "Camera stop could not be verified within %.1f seconds. "
        "Last process check:\n%s\nLast remote log tail:\n%s"
        % (timeout_sec, last_output or "<no output>", tail or "<no output>")
    )


def run_rsync(camera_host, remote_dir, local_dir, dry_run=False, timeout_sec=CAMERA_RSYNC_TIMEOUT_SEC):
    local_dir.mkdir(parents=True, exist_ok=True)
    return run_cmd(
        [
            "rsync",
            "-av",
            "--progress",
            "--partial",
            "--append-verify",
            "--timeout=30",
            f"{camera_host}:{remote_dir.rstrip('/')}/",
            str(local_dir) + "/",
        ],
        check=True,
        dry_run=dry_run,
        timeout=timeout_sec,
    )


def build_remote_camera_manifest_command(remote_video_dir):
    manifest_script = (
        "for path do "
        "size=$(stat -c '%s' \"$path\") || exit 1; "
        "hash=$(sha256sum \"$path\" | cut -d ' ' -f 1) || exit 1; "
        "printf '%s\\t%s\\t%s\\n' \"$path\" \"$size\" \"$hash\"; "
        "done"
    )
    return (
        "find %s -maxdepth 1 -type f -name %s -print0 | "
        "xargs -0 -r sh -c %s sh | sort"
        % (
            shlex.quote(remote_video_dir),
            shlex.quote(CAMERA_OUTPUT_GLOB),
            shlex.quote(manifest_script),
        )
    )


def parse_camera_manifest(stdout):
    manifest = []
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        path, size_text, digest = parts
        try:
            size_bytes = int(size_text)
        except ValueError:
            continue
        if size_bytes <= 0 or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            continue
        manifest.append(
            {
                "path": path,
                "filename": Path(path).name,
                "size_bytes": size_bytes,
                "sha256": digest.lower(),
            }
        )
    return manifest


def fetch_remote_camera_manifest(camera_host, remote_video_dir, dry_run=False):
    result = run_ssh(
        camera_host,
        build_remote_camera_manifest_command(remote_video_dir),
        dry_run=dry_run,
        timeout=CAMERA_MANIFEST_TIMEOUT_SEC,
    )
    manifest = parse_camera_manifest(result.stdout)
    if (result.stdout or "").strip() and not manifest:
        raise CameraStateError("Remote camera manifest was present but could not be parsed safely.")
    return manifest


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_camera_manifest(local_video_dir, expected_manifest=None):
    local_video_dir = Path(local_video_dir)
    raw_files = find_local_camera_raw_files(local_video_dir)
    by_name = {path.name: path for path in raw_files}
    if expected_manifest is not None:
        expected_names = {entry["filename"] for entry in expected_manifest}
        if set(by_name) != expected_names:
            raise RuntimeError(
                "Local camera raw files do not match the remote manifest: local=%s remote=%s"
                % (sorted(by_name), sorted(expected_names))
            )
    manifest = []
    for path in sorted(raw_files):
        manifest.append(
            {
                "filename": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return manifest


def verify_raw_manifest(local_video_dir, remote_manifest):
    if not remote_manifest:
        raise RuntimeError("Remote camera manifest contains no .h264 files.")
    local_manifest = local_camera_manifest(local_video_dir, remote_manifest)
    expected = {
        entry["filename"]: (entry["size_bytes"], entry["sha256"])
        for entry in remote_manifest
    }
    actual = {
        entry["filename"]: (entry["size_bytes"], entry["sha256"])
        for entry in local_manifest
    }
    if actual != expected:
        raise RuntimeError(
            "Local camera raw verification failed: local size/SHA-256 manifest does not match remote."
        )
    return local_manifest


def manifest_key(manifest):
    return sorted(
        (entry["path"], entry["filename"], entry["size_bytes"], entry["sha256"])
        for entry in manifest
    )


def validate_local_mp4_files(local_video_dir, raw_manifest):
    validations = []
    for entry in raw_manifest:
        mp4_path = Path(local_video_dir) / Path(entry["filename"]).with_suffix(".mp4")
        validation = validate_mp4_with_ffprobe(mp4_path)
        validations.append(validation)
        if not validation["validation_available"] or not validation["valid"]:
            raise RuntimeError(
                "MP4 verification failed for %s: %s" % (mp4_path, validation["error"])
            )
    return validations


def delete_remote_camera_raw_files(
    camera_host,
    remote_video_dir,
    local_video_dir,
    expected_manifest,
    dry_run=False,
):
    """Delete only the exact remote raw paths after a fresh integrity gate."""
    fresh_manifest = fetch_remote_camera_manifest(camera_host, remote_video_dir, dry_run=dry_run)
    if manifest_key(fresh_manifest) != manifest_key(expected_manifest):
        raise RuntimeError("Remote camera files changed before destructive cleanup; raw files were retained.")
    verify_raw_manifest(local_video_dir, fresh_manifest)
    validate_local_mp4_files(local_video_dir, fresh_manifest)
    if fresh_manifest:
        command = "rm -f -- " + " ".join(shlex.quote(entry["path"]) for entry in fresh_manifest)
        run_ssh(camera_host, command, dry_run=dry_run, timeout=CAMERA_RSYNC_TIMEOUT_SEC)
    return {
        "remote_raw_cleanup_attempted": True,
        "remote_raw_cleanup_completed": True,
        "remote_raw_retained": False,
        "remote_raw_cleanup_error": "",
        "remote_raw_cleanup_manifest": fresh_manifest,
    }


def saved_camera_raw_manifest(state):
    manifest = state.get("camera_raw_manifest") or state.get("remote_raw_manifest")
    if not manifest:
        raise CameraStateSchemaError(
            "Saved camera state does not contain a verified camera_raw_manifest; refusing recovery conversion."
        )
    return manifest


def find_local_camera_raw_files(local_video_dir):
    raw_files = []
    seen = set()
    for pattern in CAMERA_RAW_VIDEO_PATTERNS:
        for path in sorted(local_video_dir.glob(pattern)):
            if path in seen:
                continue
            seen.add(path)
            if path.is_file():
                raw_files.append(path)
    return raw_files


def verify_local_camera_raw_files(local_video_dir, remote_manifest=None):
    if remote_manifest is not None:
        return verify_raw_manifest(local_video_dir, remote_manifest)
    raw_files = []
    empty_files = []
    for path in find_local_camera_raw_files(local_video_dir):
        size_bytes = path.stat().st_size
        if size_bytes > 0:
            raw_files.append(path)
        else:
            empty_files.append(path)

    if empty_files:
        raise RuntimeError(
            "Empty camera raw files were found after rsync completed: %s"
            % ", ".join(str(path) for path in empty_files)
        )
    if not raw_files:
        raise RuntimeError(
            "No .h264 camera files were found in the local video directory after rsync completed."
        )
    return raw_files


class ConversionFailure(RuntimeError):
    """Conversion failed after preserving the raw input and attempt details."""

    def __init__(self, message, conversion_result):
        super().__init__(message)
        self.conversion_result = conversion_result


def _file_size_bytes(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _raw_file_is_present(path):
    return path.is_file() and _file_size_bytes(path) > 0


def _delete_partial_mp4(output_path):
    if not output_path.exists():
        return False
    try:
        output_path.unlink()
    except OSError:
        return False
    return True


def validate_mp4_with_ffprobe(mp4_path, timeout_sec=CAMERA_FFPROBE_TIMEOUT_SEC):
    """Validate an MP4 container quickly without decoding the video."""
    mp4_path = Path(mp4_path)
    result = {
        # A missing or unusable validator is distinct from a validator rejecting the file.
        "validation_available": True,
        "valid": False,
        "return_code": None,
        "duration_sec": None,
        "size_bytes": _file_size_bytes(mp4_path),
        "video_stream_count": 0,
        "width": None,
        "height": None,
        "frame_rate": None,
        "ffprobe_path": None,
        "error": "",
    }
    if result["size_bytes"] <= 0:
        result["error"] = "MP4 file is missing or empty."
        return result

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        result["validation_available"] = False
        result["valid"] = None
        result["error"] = "ffprobe is not installed or not available on PATH."
        return result
    result["ffprobe_path"] = ffprobe

    cmd = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,r_frame_rate,duration",
        "-show_entries",
        "format=duration,size",
        "-of",
        "json",
        str(mp4_path),
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            text=True,
            capture_output=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as exc:
        result["validation_available"] = False
        result["valid"] = None
        result["error"] = "ffprobe validation timed out after %.1f seconds." % timeout_sec
        return result
    except OSError as exc:
        result["validation_available"] = False
        result["valid"] = None
        result["error"] = "Could not run ffprobe: %s" % exc
        return result

    result["return_code"] = completed.returncode
    if completed.returncode != 0:
        stderr = tail_text(subprocess_output_to_text(completed.stderr)).strip()
        result["error"] = "ffprobe returned exit status %s%s" % (
            completed.returncode,
            ": %s" % stderr if stderr else "",
        )
        return result

    try:
        payload = json.loads(subprocess_output_to_text(completed.stdout) or "{}")
    except (TypeError, ValueError) as exc:
        result["error"] = "ffprobe returned invalid JSON: %s" % exc
        return result

    streams = payload.get("streams") or []
    result["video_stream_count"] = len(streams)
    if not streams:
        result["error"] = "ffprobe found no video stream."
        return result

    stream = streams[0]
    try:
        result["width"] = int(stream.get("width"))
        result["height"] = int(stream.get("height"))
    except (TypeError, ValueError):
        result["error"] = "ffprobe did not report a parseable video width and height."
        return result
    if result["width"] <= 0 or result["height"] <= 0:
        result["error"] = "ffprobe reported non-positive video dimensions."
        return result

    frame_rate_text = stream.get("r_frame_rate")
    try:
        if not frame_rate_text or "/" not in str(frame_rate_text):
            raise ValueError
        numerator, denominator = str(frame_rate_text).split("/", 1)
        frame_rate = float(numerator) / float(denominator)
        if not math.isfinite(frame_rate) or frame_rate <= 0:
            raise ValueError
        result["frame_rate"] = frame_rate
    except (TypeError, ValueError, ZeroDivisionError):
        result["error"] = "ffprobe did not report a parseable positive video frame rate."
        return result

    duration_value = None
    stream_duration = streams[0].get("duration") if streams else None
    format_duration = (payload.get("format") or {}).get("duration")
    for candidate in (stream_duration, format_duration):
        if candidate not in (None, "", "N/A"):
            duration_value = candidate
            break
    if duration_value is not None:
        try:
            duration_value = float(duration_value)
        except (TypeError, ValueError):
            result["error"] = "ffprobe reported a non-numeric duration."
            return result
        if not math.isfinite(duration_value) or duration_value <= 0:
            result["error"] = "ffprobe reported an invalid duration: %s" % duration_value
            return result
        result["duration_sec"] = duration_value
    else:
        result["error"] = "ffprobe did not report a positive video duration."
        return result

    result["valid"] = True
    return result


class ConversionFailure(RuntimeError):
    """Conversion failed after preserving the raw input and attempt details."""

    def __init__(self, message, conversion_result):
        super().__init__(message)
        self.conversion_result = conversion_result


def _file_size_bytes(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _raw_file_is_present(path):
    return path.is_file() and _file_size_bytes(path) > 0


def convert_h264_to_mp4(
    local_video_dir,
    framerate=CAMERA_FRAMERATE,
    dry_run=False,
    timeout_sec=CAMERA_FFMPEG_TIMEOUT_SEC,
    faststart=CAMERA_MP4_FASTSTART,
    event_callback=None,
):
    """Remux raw H.264 to MP4 while never modifying the raw input files."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "FFmpeg is not installed or not available on PATH; raw .h264 files were fetched but MP4 conversion could not run."
        )

    h264_files = verify_local_camera_raw_files(Path(local_video_dir))
    result = {
        "conversion_attempted": False,
        "conversion_completed": False,
        "conversion_skipped": False,
        "conversion_skip_reason": None,
        "input_files": [str(path) for path in h264_files],
        "output_files": [],
        "conversion_attempts": [],
        "validated_mp4_files": [],
        "unvalidated_mp4_files": [],
        "ffmpeg_timeout_sec": timeout_sec,
        "faststart_enabled": bool(faststart),
        "ffprobe_timeout_sec": CAMERA_FFPROBE_TIMEOUT_SEC,
    }

    if dry_run:
        result["conversion_skipped"] = True
        result["conversion_skip_reason"] = "dry_run"
        return result

    def emit(event, details):
        if event_callback is not None:
            event_callback(event, dict(details))

    attempt_number = 0
    for input_path in h264_files:
        output_path = input_path.with_suffix(".mp4")
        partial_mp4_deleted = False
        if output_path.exists() and _file_size_bytes(output_path) > 0:
            existing_validation = validate_mp4_with_ffprobe(output_path)
            if existing_validation["valid"]:
                print("Valid MP4 already exists, skipping: %s" % output_path)
                result["output_files"].append(str(output_path))
                result["validated_mp4_files"].append(str(output_path))
                emit(
                    "camera_conversion_skipped_existing_valid",
                    {
                        "mp4_path": str(output_path),
                        "mp4_size_bytes": existing_validation["size_bytes"],
                        "mp4_validation_available": True,
                        "mp4_validated": True,
                        "mp4_validation_status": "valid",
                        "ffprobe_path": existing_validation["ffprobe_path"],
                        "ffprobe_error": "",
                        "ffprobe_valid": True,
                        "duration_sec": existing_validation["duration_sec"],
                        "video_stream_count": existing_validation["video_stream_count"],
                        "ffprobe_return_code": existing_validation["return_code"],
                    },
                )
                continue

            partial_mp4_deleted = _delete_partial_mp4(output_path)
            existing_event = (
                "camera_conversion_existing_unvalidated"
                if not existing_validation["validation_available"]
                else "camera_conversion_existing_invalid"
            )
            emit(
                existing_event,
                {
                    "mp4_path": str(output_path),
                    "mp4_size_bytes": existing_validation["size_bytes"],
                    "mp4_validation_available": existing_validation["validation_available"],
                    "mp4_validated": False,
                    "mp4_validation_status": (
                        "unavailable" if not existing_validation["validation_available"] else "invalid"
                    ),
                    "ffprobe_path": existing_validation["ffprobe_path"],
                    "ffprobe_error": existing_validation["error"],
                    "ffprobe_valid": existing_validation["valid"],
                    "duration_sec": existing_validation["duration_sec"],
                    "video_stream_count": existing_validation["video_stream_count"],
                    "ffprobe_return_code": existing_validation["return_code"],
                    "error_message": existing_validation["error"],
                    "existing_mp4_invalid": bool(existing_validation["validation_available"]),
                    "existing_mp4_unvalidated": not existing_validation["validation_available"],
                    "existing_mp4_removed": partial_mp4_deleted,
                    "raw_h264_still_present": _raw_file_is_present(input_path),
                },
            )
        elif output_path.exists():
            partial_mp4_deleted = _delete_partial_mp4(output_path)

        attempt_number += 1
        input_size_bytes = _file_size_bytes(input_path)
        attempt = {
            "input_h264_path": str(input_path),
            "output_mp4_path": str(output_path),
            "input_size_bytes": input_size_bytes,
            "output_size_bytes": 0,
            "ffmpeg_timeout_sec": timeout_sec,
            "faststart_enabled": bool(faststart),
            "conversion_elapsed_sec": 0.0,
            "attempt_number": attempt_number,
            "ffmpeg_return_code": None,
            "raw_h264_still_present": _raw_file_is_present(input_path),
            "partial_mp4_deleted": partial_mp4_deleted,
            "error_message": "",
            "reason": "",
            "ffprobe_valid": None,
            "mp4_validation_available": None,
            "mp4_validated": False,
            "mp4_validation_status": "not_run",
            "ffprobe_path": None,
            "ffprobe_error": "",
            "ffprobe_return_code": None,
            "duration_sec": None,
            "video_stream_count": 0,
        }
        result["conversion_attempted"] = True
        emit("camera_conversion_started", attempt)

        cmd = [
            ffmpeg,
            "-y",
            "-framerate",
            str(framerate),
            "-i",
            str(input_path),
            "-c:v",
            "copy",
        ]
        if faststart:
            cmd.extend(["-movflags", "+faststart"])
        cmd.append(str(output_path))
        print("+ " + " ".join(shlex.quote(x) for x in cmd))

        started = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                check=True,
                text=True,
                capture_output=True,
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            attempt["conversion_elapsed_sec"] = time.monotonic() - started
            attempt["reason"] = "timeout"
            attempt["error_message"] = "MP4 conversion timed out after %.1f seconds for %s" % (
                timeout_sec,
                input_path.name,
            )
            attempt["partial_mp4_deleted"] = _delete_partial_mp4(output_path) or partial_mp4_deleted
            attempt["raw_h264_still_present"] = _raw_file_is_present(input_path)
            result["conversion_attempts"].append(dict(attempt))
            emit("camera_conversion_failed", attempt)
            print_subprocess_output(exc.stdout, exc.stderr)
            raise ConversionFailure(attempt["error_message"], result) from exc
        except subprocess.CalledProcessError as exc:
            attempt["conversion_elapsed_sec"] = time.monotonic() - started
            attempt["reason"] = "ffmpeg_nonzero_return"
            attempt["ffmpeg_return_code"] = exc.returncode
            attempt["error_message"] = "FFmpeg returned nonzero exit status %s for %s" % (
                exc.returncode,
                input_path.name,
            )
            attempt["partial_mp4_deleted"] = _delete_partial_mp4(output_path) or partial_mp4_deleted
            attempt["raw_h264_still_present"] = _raw_file_is_present(input_path)
            result["conversion_attempts"].append(dict(attempt))
            emit("camera_conversion_failed", attempt)
            print_subprocess_output(exc.stdout, exc.stderr)
            raise ConversionFailure(attempt["error_message"], result) from exc
        except KeyboardInterrupt as exc:
            attempt["conversion_elapsed_sec"] = time.monotonic() - started
            attempt["reason"] = "interrupted"
            attempt["error_message"] = "MP4 conversion interrupted for %s" % input_path.name
            attempt["partial_mp4_deleted"] = _delete_partial_mp4(output_path) or partial_mp4_deleted
            attempt["raw_h264_still_present"] = _raw_file_is_present(input_path)
            result["conversion_attempts"].append(dict(attempt))
            emit("camera_conversion_failed", attempt)
            raise ConversionFailure(attempt["error_message"], result) from exc

        attempt["conversion_elapsed_sec"] = time.monotonic() - started
        attempt["ffmpeg_return_code"] = completed.returncode
        if not output_path.exists() or _file_size_bytes(output_path) <= 0:
            attempt["reason"] = "invalid_output"
            attempt["error_message"] = "FFmpeg returned successfully but did not create a non-empty MP4: %s" % output_path
            attempt["partial_mp4_deleted"] = _delete_partial_mp4(output_path) or partial_mp4_deleted
            attempt["raw_h264_still_present"] = _raw_file_is_present(input_path)
            result["conversion_attempts"].append(dict(attempt))
            emit("camera_conversion_failed", attempt)
            raise ConversionFailure(attempt["error_message"], result)

        validation = validate_mp4_with_ffprobe(output_path)
        attempt["ffprobe_valid"] = validation["valid"]
        attempt["mp4_validation_available"] = validation["validation_available"]
        attempt["mp4_validated"] = bool(validation["valid"])
        attempt["mp4_validation_status"] = (
            "valid"
            if validation["valid"]
            else "unavailable" if not validation["validation_available"] else "invalid"
        )
        attempt["ffprobe_path"] = validation["ffprobe_path"]
        attempt["ffprobe_error"] = validation["error"]
        attempt["ffprobe_return_code"] = validation["return_code"]
        attempt["duration_sec"] = validation["duration_sec"]
        attempt["video_stream_count"] = validation["video_stream_count"]
        attempt["output_size_bytes"] = validation["size_bytes"]
        if not validation["validation_available"]:
            attempt["reason"] = "ffprobe_validation_unavailable"
            attempt["error_message"] = validation["error"]
            attempt["raw_h264_still_present"] = _raw_file_is_present(input_path)
            result["conversion_attempts"].append(dict(attempt))
            result["output_files"].append(str(output_path))
            result["unvalidated_mp4_files"].append(str(output_path))
            emit("camera_conversion_validation_unavailable", attempt)
            emit("camera_conversion_completed", attempt)
            print("Converted %s -> %s (ffprobe validation unavailable)" % (input_path.name, output_path.name))
            continue

        if not validation["valid"]:
            attempt["reason"] = "ffprobe_validation_failed"
            attempt["error_message"] = validation["error"] or "ffprobe rejected the generated MP4."
            emit("camera_conversion_validation_failed", attempt)
            attempt["partial_mp4_deleted"] = _delete_partial_mp4(output_path) or partial_mp4_deleted
            attempt["raw_h264_still_present"] = _raw_file_is_present(input_path)
            result["conversion_attempts"].append(dict(attempt))
            emit("camera_conversion_failed", attempt)
            raise ConversionFailure(attempt["error_message"], result)

        attempt["raw_h264_still_present"] = _raw_file_is_present(input_path)
        result["conversion_attempts"].append(dict(attempt))
        result["output_files"].append(str(output_path))
        result["validated_mp4_files"].append(str(output_path))
        emit("camera_conversion_completed", attempt)
        print("Converted %s -> %s" % (input_path.name, output_path.name))

    result["conversion_completed"] = (
        bool(result["validated_mp4_files"])
        and len(result["validated_mp4_files"]) == len(h264_files)
        and not result["unvalidated_mp4_files"]
    )
    result["mp4_verified"] = result["conversion_completed"]
    return result

def run_camera_conversion_workflow(
    camera_host,
    local_video_dir,
    framerate=CAMERA_FRAMERATE,
    dry_run=False,
    timeout_sec=CAMERA_FFMPEG_TIMEOUT_SEC,
    faststart=CAMERA_MP4_FASTSTART,
):
    local_video_dir = Path(local_video_dir)
    result = {
        "camera_raw_files_verified": False,
        "camera_raw_file_count": 0,
        "raw_h264_available_locally": False,
        "camera_conversion_attempted": False,
        "camera_conversion_completed": False,
        "camera_conversion_deferred": False,
        "camera_conversion_error": "",
        "camera_conversion_skip_reason": "",
        "camera_mp4_verified": False,
        "converted_mp4_files": [],
        "conversion_result": None,
        "conversion_attempts": [],
        "camera_mp4_faststart": bool(faststart),
    }

    try:
        raw_files = verify_local_camera_raw_files(local_video_dir)
    except Exception as exc:
        append_event(
            local_video_dir,
            "camera_raw_verification_failed",
            {
                "camera_host": camera_host,
                "local_video_dir": str(local_video_dir),
                "error": str(exc),
            },
        )
        raise

    result["camera_raw_files_verified"] = True
    result["camera_raw_file_count"] = len(raw_files)
    result["raw_h264_available_locally"] = True
    append_event(
        local_video_dir,
        "camera_raw_files_verified",
        {
            "camera_host": camera_host,
            "local_video_dir": str(local_video_dir),
            "raw_file_count": len(raw_files),
            "raw_files": [str(path) for path in raw_files],
        },
    )

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        error = (
            "FFmpeg is not installed or not available on PATH; raw .h264 files were fetched but MP4 conversion could not run."
        )
        result["camera_conversion_deferred"] = True
        result["camera_conversion_error"] = error
        result["camera_conversion_skip_reason"] = "ffmpeg_not_available"
        append_event(
            local_video_dir,
            "camera_conversion_deferred",
            {
                "camera_host": camera_host,
                "local_video_dir": str(local_video_dir),
                "reason": "ffmpeg_not_available",
                "raw_h264_available_locally": True,
                "raw_h264_still_present": all(_raw_file_is_present(path) for path in raw_files),
                "error": error,
            },
        )
        print(error, file=sys.stderr)
        return result

    def record_conversion_event(event, details):
        event_details = {
            "camera_host": camera_host,
            "local_video_dir": str(local_video_dir),
        }
        event_details.update(details)
        append_event(local_video_dir, event, event_details)

    try:
        conversion_result = convert_h264_to_mp4(
            local_video_dir,
            framerate=framerate,
            dry_run=dry_run,
            timeout_sec=timeout_sec,
            faststart=faststart,
            event_callback=record_conversion_event,
        )
    except ConversionFailure as exc:
        conversion_result = exc.conversion_result
        result["conversion_result"] = conversion_result
        result["conversion_attempts"] = list(conversion_result.get("conversion_attempts", []))
        result["camera_conversion_attempted"] = bool(result["conversion_attempts"])
        result["camera_conversion_deferred"] = True
        result["camera_conversion_error"] = str(exc)
        failed_attempt = result["conversion_attempts"][-1] if result["conversion_attempts"] else {}
        result["camera_conversion_skip_reason"] = failed_attempt.get("reason", "conversion_failed")
        result["raw_h264_still_present"] = all(_raw_file_is_present(path) for path in raw_files)
        print("MP4 conversion did not complete.", file=sys.stderr)
        return result
    except Exception as exc:
        result["camera_conversion_deferred"] = True
        result["camera_conversion_error"] = str(exc)
        result["camera_conversion_skip_reason"] = "conversion_failed"
        result["raw_h264_still_present"] = all(_raw_file_is_present(path) for path in raw_files)
        append_event(
            local_video_dir,
            "camera_conversion_failed",
            {
                "camera_host": camera_host,
                "local_video_dir": str(local_video_dir),
                "ffmpeg_timeout_sec": timeout_sec,
                "faststart_enabled": bool(faststart),
                "conversion_elapsed_sec": 0.0,
                "attempt_number": 0,
                "ffmpeg_return_code": None,
                "raw_h264_still_present": result["raw_h264_still_present"],
                "partial_mp4_deleted": False,
                "error_message": str(exc),
                "reason": "conversion_failed",
            },
        )
        print("MP4 conversion did not complete.", file=sys.stderr)
        return result

    result["conversion_result"] = conversion_result
    result["conversion_attempts"] = list(conversion_result.get("conversion_attempts", []))
    result["camera_conversion_attempted"] = bool(conversion_result.get("conversion_attempted"))
    result["camera_conversion_completed"] = bool(conversion_result.get("conversion_completed"))
    result["camera_conversion_deferred"] = not result["camera_conversion_completed"]
    result["camera_conversion_skip_reason"] = conversion_result.get("conversion_skip_reason") or ""
    result["converted_mp4_files"] = list(conversion_result.get("output_files", []))
    result["camera_mp4_verified"] = bool(conversion_result.get("mp4_verified"))
    result["raw_h264_still_present"] = all(_raw_file_is_present(path) for path in raw_files)

    if not result["camera_conversion_completed"]:
        append_event(
            local_video_dir,
            "camera_conversion_deferred",
            {
                "camera_host": camera_host,
                "local_video_dir": str(local_video_dir),
                "reason": result["camera_conversion_skip_reason"] or "dry_run",
                "raw_h264_available_locally": True,
                "raw_h264_still_present": result["raw_h264_still_present"],
                "faststart_enabled": bool(faststart),
            },
        )

    return result

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


def atomic_write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=".%s." % path.name,
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(path))
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def write_video_manifest(state):
    """Write the per-session archival integrity projection atomically."""
    local_video_dir_value = state.get("local_video_dir")
    if not local_video_dir_value:
        return None
    local_video_dir = Path(local_video_dir_value)

    try:
        local_raw_manifest = local_camera_manifest(local_video_dir)
    except (OSError, ValueError):
        local_raw_manifest = []

    conversion_result = state.get("conversion_result") or {}
    manifest = {
        "session_id": state.get("session_id"),
        "camera_host": state.get("camera_host"),
        "remote_video_dir": state.get("remote_video_dir"),
        "remote_raw_manifest": state.get("camera_raw_manifest")
        or state.get("remote_raw_manifest_before_transfer")
        or [],
        "local_raw_manifest": local_raw_manifest,
        "camera_raw_files_verified": bool(state.get("camera_raw_files_verified")),
        "camera_raw_hash_verified": bool(state.get("camera_raw_hash_verified")),
        "camera_mp4_verified": bool(state.get("camera_mp4_verified")),
        "converted_mp4_files": state.get("converted_mp4_files") or [],
        "ffprobe_validation_results": state.get("ffprobe_validation_results")
        or conversion_result.get("conversion_attempts")
        or [],
        "camera_transfer_command_completed": bool(state.get("camera_transfer_command_completed")),
        "camera_fetch_completed": bool(state.get("camera_fetch_completed")),
        "camera_conversion_completed": bool(state.get("camera_conversion_completed")),
        "remote_raw_cleanup_attempted": bool(state.get("remote_raw_cleanup_attempted")),
        "remote_raw_cleanup_completed": bool(state.get("remote_raw_cleanup_completed")),
        "remote_raw_retained": bool(state.get("remote_raw_retained", True)),
        "remote_raw_cleanup_error": state.get("remote_raw_cleanup_error", ""),
        "updated_utc": utc_iso_now(),
    }
    manifest_path = local_video_dir / VIDEO_MANIFEST_FILENAME
    atomic_write_json(manifest_path, manifest)
    return manifest_path


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(STATE_FILE, state)
    write_video_manifest(state)
    print("Saved state: %s" % STATE_FILE)


def validate_camera_state(state, source_path):
    if not isinstance(state, dict):
        raise CameraStateCorruptError("Camera state at %s is not a JSON object." % source_path)
    if state.get("controller_repository") != CONTROLLER_REPOSITORY:
        raise CameraStateOwnershipError(
            "Camera state at %s is not owned by %s." % (source_path, CONTROLLER_REPOSITORY)
        )
    if state.get("protocol_name") != PROTOCOL_NAME:
        raise CameraStateOwnershipError(
            "Camera state at %s has an unexpected protocol identity." % source_path
        )
    if state.get("state_schema_version") != CAMERA_STATE_SCHEMA_VERSION:
        raise CameraStateSchemaError(
            "Camera state at %s has unsupported schema version %r." % (source_path, state.get("state_schema_version"))
        )
    required_fields = (
        "session_id",
        "mouse_id",
        "camera_host",
        "remote_session_dir",
        "remote_video_dir",
        "remote_base_path",
        "local_session_dir",
        "local_video_dir",
        "camera_pid",
        "remote_pid_file",
        "camera_start_requested_utc",
        "camera_start_confirmed_utc",
        "camera_stop_confirmed_utc",
    )
    missing = [field for field in required_fields if field not in state]
    if missing:
        raise CameraStateSchemaError(
            "Camera state at %s is missing required fields: %s" % (source_path, ", ".join(missing))
        )
    return state


def load_state():
    if STATE_FILE.exists():
        state_file = STATE_FILE
    elif GENERIC_STATE_FILE.exists() or LEGACY_STATE_FILE.exists():
        legacy_path = GENERIC_STATE_FILE if GENERIC_STATE_FILE.exists() else LEGACY_STATE_FILE
        raise CameraStateOwnershipError(
            "Legacy generic camera state exists at %s. Automatic destructive recovery is disabled because ownership cannot be proven. "
            "Use explicit --mouse-id and --session-id recovery arguments or migrate the state manually." % legacy_path
        )
    else:
        raise CameraStateNotFoundError(
            "No saved camera session state found at %s.\n"
            "Also checked legacy location %s.\n"
            "Run `python3 remote_camera_control.py start --mouse-id <mouse_id>` first, "
            "or pass `--mouse-id` and `--session-id` to fetch/stop-fetch."
            % (STATE_FILE, GENERIC_STATE_FILE)
        )
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise CameraStateCorruptError("Could not read valid JSON camera state at %s: %s" % (state_file, exc)) from exc
    return validate_camera_state(state, state_file)


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
        "state_schema_version": CAMERA_STATE_SCHEMA_VERSION,
        "controller_repository": CONTROLLER_REPOSITORY,
        "protocol_name": PROTOCOL_NAME,
        "created_utc": utc_iso_now(),
        "camera_host": camera_host,
        "framerate": getattr(args, "framerate", CAMERA_FRAMERATE),
        "remote_camera_repo": getattr(args, "remote_camera_repo", REMOTE_CAMERA_REPO),
        "remote_camera_start": getattr(args, "remote_camera_start", REMOTE_CAMERA_START),
        "remote_camera_stop": getattr(args, "remote_camera_stop", REMOTE_CAMERA_STOP),
        "camera_pid": None,
        "camera_start_requested_utc": None,
        "camera_start_confirmed_utc": None,
        "camera_stop_confirmed_utc": None,
        "camera_fetch_status": "not_started",
        "remote_raw_cleanup_attempted": False,
        "remote_raw_cleanup_completed": False,
        "remote_raw_retained": True,
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
        "state_schema_version": CAMERA_STATE_SCHEMA_VERSION,
        "controller_repository": CONTROLLER_REPOSITORY,
        "protocol_name": PROTOCOL_NAME,
        "created_utc": utc_iso_now(),
        "camera_host": camera_host,
        "framerate": args.framerate,
        "remote_camera_repo": args.remote_camera_repo,
        "remote_camera_start": args.remote_camera_start,
        "remote_camera_stop": args.remote_camera_stop,
        "camera_status": "launch_requested",
        "camera_pid": None,
        "camera_start_command_returned": False,
        "camera_pid_confirmed": False,
        "camera_process_confirmed": False,
        "camera_output_file_detected": False,
        "camera_output_growing_confirmed": False,
        "camera_start_failed": False,
        "camera_start_requested_utc": utc_iso_now(),
        "camera_launch_returned_utc": None,
        "camera_output_growth_confirmed_utc": None,
        "camera_start_confirmed_utc": None,
        "camera_stop_confirmed_utc": None,
        "camera_fetch_status": "not_started",
        "remote_raw_cleanup_attempted": False,
        "remote_raw_cleanup_completed": False,
        "remote_raw_retained": True,
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
        state["camera_start_command_returned"] = True
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
    except Exception as exc:
        if "timed out" in str(exc).lower():
            launch_timed_out = True
            state["camera_launch_timed_out"] = True
            print("Camera launch SSH timed out; checking whether the remote process still started.")
            save_state(state)
        else:
            state["camera_start_failed"] = True
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
            try:
                state["camera_cleanup_after_start_failure_attempted"] = True
                stop_camera(args, state)
                state["camera_cleanup_after_start_failure_confirmed"] = True
                save_state(state)
            except Exception as cleanup_exc:
                state["camera_cleanup_after_start_failure_confirmed"] = False
                state["camera_cleanup_after_start_failure_error"] = str(cleanup_exc)
                save_state(state)
                raise RuntimeError(
                    "Camera launch failed, and cleanup could not be confirmed: %s"
                    % cleanup_exc
                ) from exc
            raise RuntimeError("Camera launch failed; cleanup was confirmed.") from exc

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
        state["camera_status"] = "process_confirmed"
        state["camera_pid_confirmed"] = True
        state["camera_process_confirmed"] = True
        state["camera_process_confirmed_utc"] = utc_iso_now()
        save_state(state)
        append_event(
            local_video_dir,
            "camera_process_confirmed",
            {
                "camera_host": camera_host,
                "camera_pid": state.get("camera_pid"),
                "remote_pid_file": remote_pid_file,
            },
        )
        print("Camera acquisition process is running; waiting for video output...")

        output_ready = wait_for_remote_camera_output_growth(
            camera_host,
            remote_pid_file,
            paths["remote_video_dir"],
            remote_log,
            dry_run=args.dry_run,
            timeout_sec=CAMERA_OUTPUT_READY_TIMEOUT_SEC,
        )
        state["camera_status"] = "recording_confirmed"
        state["camera_output_file_detected"] = output_ready["camera_output_file_detected"]
        state["camera_output_growing_confirmed"] = output_ready["camera_output_growing_confirmed"]
        state["camera_output_file"] = output_ready["camera_output_file"]
        state["camera_output_initial_size_bytes"] = output_ready["initial_size_bytes"]
        state["camera_output_confirmed_size_bytes"] = output_ready["confirmed_size_bytes"]
        state["camera_readiness_reference"] = "pid_alive_and_output_size_increasing"
        state["camera_output_growth_confirmed_utc"] = utc_iso_now()
        state["camera_start_confirmed_utc"] = state["camera_output_growth_confirmed_utc"]
        state["camera_launch_timed_out"] = launch_timed_out
        save_state(state)
        append_event(
            local_video_dir,
            "camera_recording_confirmed",
            {
                "camera_host": camera_host,
                "camera_pid": state.get("camera_pid"),
                "camera_pid_confirmed": True,
                "camera_process_confirmed": True,
                "camera_output_file_detected": True,
                "camera_output_growing_confirmed": True,
                "camera_output_file": state.get("camera_output_file"),
                "camera_readiness_reference": state["camera_readiness_reference"],
                "camera_launch_timed_out": launch_timed_out,
                "remote_pid_file": remote_pid_file,
                "remote_log_file": remote_log,
            },
        )
    except Exception as exc:
        tail = tail_remote_log(camera_host, remote_log, dry_run=args.dry_run)
        print("Camera startup failed. Remote log tail\n%s" % (tail or "<no log output>"), file=sys.stderr)
        state["camera_start_failed"] = True
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
            state["camera_cleanup_after_start_failure_attempted"] = True
            stop_camera(args, state)
            state["camera_cleanup_after_start_failure_confirmed"] = True
            save_state(state)
        except Exception as cleanup_exc:
            state["camera_cleanup_after_start_failure_confirmed"] = False
            state["camera_cleanup_after_start_failure_error"] = str(cleanup_exc)
            save_state(state)
            raise RuntimeError(
                "Camera recording did not become ready, and cleanup could not be confirmed: %s"
                % cleanup_exc
            ) from exc
        raise RuntimeError("Camera recording did not become ready; cleanup was confirmed.") from exc

    print("Camera video output confirmed growing.")
    print("Camera host:      %s" % camera_host)
    print("Remote video dir: %s" % paths["remote_video_dir"])
    print("Local video dir:  %s" % local_video_dir)
    return state


def stop_camera(args, state=None):
    if state is None:
        state = load_state()
    else:
        state = validate_camera_state(state, "in-memory state")

    camera_host = resolve_camera_host(args, state)
    local_video_dir = Path(state.get("local_video_dir", LOCAL_VIDEO_ROOT / "unknown" / "video"))
    local_video_dir.mkdir(parents=True, exist_ok=True)
    remote_pid_file = state.get("remote_pid_file") or "%s/camera_acquisition.pid" % state.get("remote_video_dir", REMOTE_VIDEO_ROOT)
    remote_video_dir = state.get("remote_video_dir") or REMOTE_VIDEO_ROOT
    remote_base_path = state.get("remote_base_path") or remote_video_dir
    remote_log_file = state.get("remote_log_file") or "%s/camera_acquisition.log" % remote_video_dir
    expected_pid = state.get("camera_pid")
    ignore_stop_errors = getattr(args, "ignore_stop_errors", False)

    append_event(local_video_dir, "camera_stop_requested", {"camera_host": camera_host, "remote_pid_file": remote_pid_file})
    state["camera_status"] = "stop_requested"
    state["camera_stop_requested_utc"] = utc_iso_now()
    save_state(state)

    remote_stop = getattr(args, "remote_camera_stop", None) or state.get("remote_camera_stop") or REMOTE_CAMERA_STOP
    stop_cmd = build_remote_camera_stop_command(remote_pid_file, remote_stop)
    run_ssh(
        camera_host,
        stop_cmd,
        check=not ignore_stop_errors,
        dry_run=args.dry_run,
        timeout=CAMERA_STOP_TIMEOUT_SEC,
    )

    state["camera_stop_command_returned"] = True
    state["camera_stop_command_returned_utc"] = utc_iso_now()
    state["camera_stop_returned_utc"] = state["camera_stop_command_returned_utc"]
    save_state(state)
    append_event(
        local_video_dir,
        "camera_stop_command_returned",
        {"camera_host": camera_host, "remote_pid_file": remote_pid_file},
    )

    try:
        wait_for_remote_camera_stopped(
            camera_host,
            expected_pid,
            remote_base_path,
            remote_log_file,
            dry_run=args.dry_run,
            timeout_sec=CAMERA_STOP_VERIFY_TIMEOUT_SEC,
        )
    except Exception as exc:
        state["camera_status"] = "stop_unverified"
        state["camera_stop_confirmed"] = False
        state["camera_stop_verification_error"] = str(exc)
        save_state(state)
        append_event(
            local_video_dir,
            "camera_stop_unverified",
            {
                "camera_host": camera_host,
                "remote_pid_file": remote_pid_file,
                "remote_base_path": remote_base_path,
                "error": str(exc),
            },
        )
        if ignore_stop_errors:
            print(
                "WARNING: Camera stop command returned, but stopped state was not verified.",
                file=sys.stderr,
            )
            return state
        raise

    run_ssh(
        camera_host,
        "rm -f %s" % shlex.quote(remote_pid_file),
        dry_run=args.dry_run,
        timeout=CAMERA_STOP_TIMEOUT_SEC,
    )
    state["camera_status"] = "stopped_confirmed"
    state["camera_stop_confirmed"] = True
    state["camera_stop_confirmed_utc"] = utc_iso_now()
    save_state(state)
    append_event(
        local_video_dir,
        "camera_stop_confirmed",
        {
            "camera_host": camera_host,
            "remote_pid_file": remote_pid_file,
            "remote_base_path": remote_base_path,
            "expected_pid": expected_pid,
        },
    )
    print("Camera stop verified.")
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
        preview_error = None
        while True:
            try:
                response = input("> ").strip().lower()
            except EOFError as exc:
                preview_error = RuntimeError(
                    "Preview input ended at EOF; preview was stopped without treating EOF as confirmation."
                )
                break
            if response == "y":
                break
            print("Preview still running. Type y and Enter to stop.")
        try:
            run_ssh(camera_host, stop_cmd, dry_run=args.dry_run, timeout=CAMERA_PREVIEW_STOP_TIMEOUT_SEC)
        except Exception as cleanup_exc:
            if preview_error is not None:
                raise RuntimeError("Preview input ended and controlled preview cleanup failed: %s" % cleanup_exc) from preview_error
            raise
        if preview_error is not None:
            raise preview_error
        print("Preview stopped.")
    else:
        print("Dry run finished; preview was not actually started.")


def convert_camera(args, state=None):
    if getattr(args, "ffmpeg_timeout_sec", CAMERA_FFMPEG_TIMEOUT_SEC) <= 0:
        raise ValueError("--ffmpeg-timeout-sec must be greater than 0")

    if state is None:
        try:
            state = load_state()
        except CameraStateNotFoundError:
            state = build_state_from_args(args)
    else:
        state = validate_camera_state(state, "in-memory state")

    camera_host = resolve_camera_host(args, state)
    local_video_dir = Path(state["local_video_dir"])
    local_video_dir.mkdir(parents=True, exist_ok=True)

    append_event(
        local_video_dir,
        "camera_conversion_requested",
        {
            "camera_host": camera_host,
            "local_video_dir": str(local_video_dir),
            "ffmpeg_timeout_sec": getattr(args, "ffmpeg_timeout_sec", CAMERA_FFMPEG_TIMEOUT_SEC),
        },
    )

    def record_integrity_failure(error):
        state["camera_raw_files_verified"] = False
        state["camera_raw_hash_verified"] = False
        state["camera_conversion_completed"] = False
        state["camera_conversion_deferred"] = True
        state["camera_mp4_verified"] = False
        state["camera_conversion_failed"] = True
        state["camera_conversion_failed_utc"] = utc_iso_now()
        state["camera_conversion_error"] = str(error)
        state["remote_raw_retained"] = True
        state["remote_raw_cleanup_completed"] = bool(state.get("remote_raw_cleanup_completed"))
        save_state(state)
        append_event(
            local_video_dir,
            "camera_conversion_integrity_failed",
            {
                "camera_host": camera_host,
                "local_video_dir": str(local_video_dir),
                "error": str(error),
                "remote_raw_retained": True,
            },
        )
        print("Camera raw integrity verification failed; MP4 conversion and remote cleanup were not run.", file=sys.stderr)
        return state

    try:
        raw_manifest = saved_camera_raw_manifest(state)
        verify_raw_manifest(local_video_dir, raw_manifest)
    except Exception as exc:
        return record_integrity_failure(exc)

    state["camera_raw_files_verified"] = True
    state["camera_raw_hash_verified"] = True
    state["camera_raw_manifest"] = raw_manifest
    save_state(state)

    try:
        conversion_state = run_camera_conversion_workflow(
            camera_host,
            local_video_dir,
            framerate=state.get("framerate", CAMERA_FRAMERATE),
            dry_run=args.dry_run,
            timeout_sec=getattr(args, "ffmpeg_timeout_sec", CAMERA_FFMPEG_TIMEOUT_SEC),
        )
    except Exception as exc:
        state["camera_conversion_failed"] = True
        state["camera_conversion_failed_utc"] = utc_iso_now()
        state["camera_conversion_error"] = str(exc)
        state["camera_conversion_completed"] = False
        state["camera_conversion_deferred"] = True
        state["camera_mp4_verified"] = False
        save_state(state)
        append_event(
            local_video_dir,
            "camera_conversion_failed",
            {
                "camera_host": camera_host,
                "local_video_dir": str(local_video_dir),
                "ffmpeg_timeout_sec": getattr(args, "ffmpeg_timeout_sec", CAMERA_FFMPEG_TIMEOUT_SEC),
                "error": str(exc),
            },
        )
        print("MP4 conversion did not complete.", file=sys.stderr)
        return state

    state.update(conversion_state)
    state["camera_conversion_completed_utc"] = utc_iso_now() if conversion_state.get("camera_conversion_completed") else ""
    state["camera_conversion_failed"] = False
    state["camera_conversion_failed_utc"] = ""
    state["camera_mp4_verified"] = bool(conversion_state.get("camera_mp4_verified"))
    state["camera_conversion_deferred"] = not state["camera_mp4_verified"]
    state["remote_raw_retained"] = not bool(state.get("remote_raw_cleanup_completed"))

    if state["camera_mp4_verified"] and not state.get("remote_raw_cleanup_completed"):
        state["remote_raw_cleanup_attempted"] = True
        save_state(state)
        try:
            cleanup_state = delete_remote_camera_raw_files(
                camera_host,
                state["remote_video_dir"],
                local_video_dir,
                raw_manifest,
                dry_run=args.dry_run,
            )
            state.update(cleanup_state)
        except Exception as exc:
            state["remote_raw_cleanup_completed"] = False
            state["remote_raw_retained"] = True
            state["remote_raw_cleanup_error"] = str(exc)
            save_state(state)
            append_event(
                local_video_dir,
                "remote_raw_cleanup_failed",
                {
                    "camera_host": camera_host,
                    "remote_video_dir": state["remote_video_dir"],
                    "error": str(exc),
                    "raw_h264_retained": True,
                },
            )
            raise

    state["camera_data_secured"] = bool(
        state.get("camera_stop_confirmed")
        and state.get("camera_raw_hash_verified")
        and state.get("camera_mp4_verified")
        and state.get("remote_raw_cleanup_completed")
    )
    save_state(state)
    if conversion_state.get("camera_conversion_completed"):
        print("Converted camera files in: %s" % local_video_dir)
    return state

def fetch_camera(args, state=None):
    if getattr(args, "rsync_timeout_sec", CAMERA_RSYNC_TIMEOUT_SEC) <= 0:
        raise ValueError("--rsync-timeout-sec must be greater than 0")
    if getattr(args, "ffmpeg_timeout_sec", CAMERA_FFMPEG_TIMEOUT_SEC) <= 0:
        raise ValueError("--ffmpeg-timeout-sec must be greater than 0")

    if state is None:
        try:
            state = load_state()
        except CameraStateNotFoundError:
            state = build_state_from_args(args)
    else:
        state = validate_camera_state(state, "in-memory state")

    remote_video_dir = state["remote_video_dir"]
    local_video_dir = Path(state["local_video_dir"])
    local_video_dir.mkdir(parents=True, exist_ok=True)

    # Once remote cleanup succeeded, the local manifest is the only safe
    # recovery source; avoid touching the camera Pi again.
    if state.get("remote_raw_cleanup_completed") and state.get("camera_raw_manifest"):
        try:
            raw_manifest = saved_camera_raw_manifest(state)
            verify_raw_manifest(local_video_dir, raw_manifest)
            ffprobe_results = validate_local_mp4_files(local_video_dir, raw_manifest)
        except Exception as exc:
            state["camera_fetch_status"] = "integrity_failed_after_cleanup"
            state["camera_fetch_completed"] = False
            state["camera_raw_files_verified"] = False
            state["camera_raw_hash_verified"] = False
            state["camera_conversion_completed"] = False
            state["camera_conversion_deferred"] = True
            state["camera_mp4_verified"] = False
            state["camera_fetch_error"] = str(exc)
            state["remote_raw_retained"] = False
            save_state(state)
            append_event(
                local_video_dir,
                "camera_fetch_integrity_failed_after_cleanup",
                {
                    "error": str(exc),
                    "remote_raw_cleanup_completed": True,
                    "remote_raw_retained": False,
                },
            )
            raise RuntimeError(
                "Remote raw files were already cleaned up, but the local camera archive no longer matches its saved manifest: %s"
                % exc
            ) from exc

        state["camera_fetch_status"] = "already_secured"
        state["camera_transfer_command_completed"] = True
        state["camera_fetch_completed"] = True
        state["camera_raw_files_verified"] = True
        state["camera_raw_hash_verified"] = True
        state["camera_raw_file_count"] = len(raw_manifest)
        state["camera_conversion_completed"] = True
        state["camera_conversion_deferred"] = False
        state["camera_mp4_verified"] = True
        state["ffprobe_validation_results"] = ffprobe_results
        state["remote_raw_cleanup_completed"] = True
        state["remote_raw_retained"] = False
        state["camera_fetch_error"] = ""
        state["camera_data_secured"] = bool(
            state.get("camera_stop_confirmed")
            and state.get("camera_raw_hash_verified")
            and state.get("camera_mp4_verified")
            and state.get("remote_raw_cleanup_completed")
        )
        save_state(state)
        print("Camera files are already secured locally; no remote transfer was needed.")
        return state

    camera_host = resolve_camera_host(args, state)
    state["camera_transfer_command_completed"] = False
    state["camera_fetch_completed"] = False
    state["camera_conversion_attempted"] = False
    state["camera_conversion_completed"] = False
    state["camera_conversion_deferred"] = False
    state["camera_fetch_timed_out"] = False
    state["camera_fetch_status"] = "requested"
    state["camera_raw_files_verified"] = False
    state["camera_raw_hash_verified"] = False
    state["raw_h264_available_locally"] = False
    state["converted_mp4_files"] = []
    state["remote_raw_cleanup_completed"] = False
    state["remote_raw_cleanup_attempted"] = False
    state["remote_raw_retained"] = True
    save_state(state)

    append_event(
        local_video_dir,
        "camera_fetch_requested",
        {
            "camera_host": camera_host,
            "remote_video_dir": remote_video_dir,
            "local_video_dir": str(local_video_dir),
            "rsync_timeout_sec": getattr(args, "rsync_timeout_sec", CAMERA_RSYNC_TIMEOUT_SEC),
            "ffmpeg_timeout_sec": getattr(args, "ffmpeg_timeout_sec", CAMERA_FFMPEG_TIMEOUT_SEC),
        },
    )

    try:
        remote_manifest_before = fetch_remote_camera_manifest(
            camera_host, remote_video_dir, dry_run=args.dry_run
        )
        state["remote_raw_manifest_before_transfer"] = remote_manifest_before
        run_rsync(
            camera_host,
            remote_video_dir,
            local_video_dir,
            dry_run=args.dry_run,
            timeout_sec=getattr(args, "rsync_timeout_sec", CAMERA_RSYNC_TIMEOUT_SEC),
        )
    except Exception as exc:
        state["camera_fetch_failed"] = True
        state["camera_fetch_failed_utc"] = utc_iso_now()
        state["camera_fetch_error"] = str(exc)
        if "timed out" in str(exc).lower():
            state["camera_fetch_timed_out"] = True
        if "manifest" in str(exc).lower() or "sha" in str(exc).lower():
            state["remote_raw_cleanup_deferred"] = True
            state["remote_raw_retained"] = True
        append_event(
            local_video_dir,
            "camera_fetch_failed",
            {
                "camera_host": camera_host,
                "remote_video_dir": remote_video_dir,
                "local_video_dir": str(local_video_dir),
                "stage": "rsync",
                "timeout_sec": getattr(args, "rsync_timeout_sec", CAMERA_RSYNC_TIMEOUT_SEC),
                "error": str(exc),
            },
        )
        save_state(state)
        raise

    try:
        remote_manifest_after = fetch_remote_camera_manifest(
            camera_host, remote_video_dir, dry_run=args.dry_run
        )
        if remote_manifest_before:
            if manifest_key(remote_manifest_after) != manifest_key(remote_manifest_before):
                raise RuntimeError("Remote camera files changed during transfer; raw files were retained.")
            verify_raw_manifest(local_video_dir, remote_manifest_after)
            state["camera_raw_hash_verified"] = True
            state["camera_raw_manifest"] = remote_manifest_after
        else:
            raise RuntimeError("Remote camera manifest is empty and no prior raw verification is recorded.")
    except Exception as exc:
        state["camera_fetch_failed"] = True
        state["camera_fetch_failed_utc"] = utc_iso_now()
        state["camera_fetch_error"] = str(exc)
        if "manifest" in str(exc).lower() or "sha" in str(exc).lower():
            state["remote_raw_cleanup_deferred"] = True
            state["remote_raw_retained"] = True
        append_event(
            local_video_dir,
            "camera_raw_verification_failed",
            {
                "camera_host": camera_host,
                "remote_video_dir": remote_video_dir,
                "local_video_dir": str(local_video_dir),
                "stage": "raw_verification",
                "error": str(exc),
            },
        )
        save_state(state)
        raise

    state["camera_transfer_command_completed"] = True
    state["camera_raw_files_verified"] = True
    state["camera_raw_file_count"] = len(find_local_camera_raw_files(local_video_dir))
    state["camera_fetch_returned_utc"] = utc_iso_now()
    state["camera_fetch_status"] = "transferred"
    save_state(state)
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
            "ffmpeg_timeout_sec": getattr(args, "ffmpeg_timeout_sec", CAMERA_FFMPEG_TIMEOUT_SEC),
        },
    )

    try:
        conversion_state = run_camera_conversion_workflow(
            camera_host,
            local_video_dir,
            framerate=state.get("framerate", CAMERA_FRAMERATE),
            dry_run=args.dry_run,
            timeout_sec=getattr(args, "ffmpeg_timeout_sec", CAMERA_FFMPEG_TIMEOUT_SEC),
        )
    except Exception as exc:
        state["camera_fetch_failed"] = True
        state["camera_fetch_failed_utc"] = utc_iso_now()
        state["camera_fetch_error"] = str(exc)
        append_event(
            local_video_dir,
            "camera_fetch_failed",
            {
                "camera_host": camera_host,
                "remote_video_dir": remote_video_dir,
                "local_video_dir": str(local_video_dir),
                "stage": "raw_verification",
                "error": str(exc),
            },
        )
        save_state(state)
        raise

    state.update(conversion_state)
    state["camera_fetch_completed"] = True
    state["camera_fetch_completed_utc"] = utc_iso_now()
    state["raw_h264_available_locally"] = bool(conversion_state.get("raw_h264_available_locally"))
    state["camera_raw_files_verified"] = bool(conversion_state.get("camera_raw_files_verified"))
    state["camera_raw_file_count"] = conversion_state.get("camera_raw_file_count", 0)
    state["camera_conversion_attempted"] = bool(conversion_state.get("camera_conversion_attempted"))
    state["camera_conversion_completed"] = bool(conversion_state.get("camera_conversion_completed"))
    state["camera_conversion_deferred"] = bool(conversion_state.get("camera_conversion_deferred"))
    state["camera_conversion_error"] = conversion_state.get("camera_conversion_error", "")
    state["camera_conversion_skip_reason"] = conversion_state.get("camera_conversion_skip_reason", "")
    state["converted_mp4_files"] = conversion_state.get("converted_mp4_files", [])
    state["camera_mp4_verified"] = bool(conversion_state.get("camera_mp4_verified"))
    state["ffprobe_validation_results"] = conversion_state.get("conversion_attempts", [])
    state["remote_raw_retained"] = True
    if state["camera_raw_hash_verified"] and state["camera_mp4_verified"]:
        state["remote_raw_cleanup_attempted"] = True
        try:
            cleanup_state = delete_remote_camera_raw_files(
                camera_host,
                remote_video_dir,
                local_video_dir,
                state.get("camera_raw_manifest", []),
                dry_run=args.dry_run,
            )
            state.update(cleanup_state)
        except Exception as exc:
            state["remote_raw_cleanup_completed"] = False
            state["remote_raw_cleanup_error"] = str(exc)
            state["remote_raw_retained"] = True
            append_event(
                local_video_dir,
                "remote_raw_cleanup_failed",
                {
                    "camera_host": camera_host,
                    "remote_video_dir": remote_video_dir,
                    "error": str(exc),
                    "raw_h264_retained": True,
                },
            )
            save_state(state)
            raise
    state["camera_fetch_status"] = "secured" if state.get("remote_raw_cleanup_completed") else "deferred"
    state["camera_data_secured"] = bool(
        state.get("camera_stop_confirmed")
        and state.get("camera_raw_hash_verified")
        and state.get("camera_mp4_verified")
        and state.get("remote_raw_cleanup_completed")
    )
    save_state(state)
    print("Fetched camera files to: %s" % local_video_dir)
    return state

def status_camera(args):
    try:
        state = load_state()
    except CameraStateNotFoundError:
        state = None
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
    common.add_argument("--json", action="store_true", help="Also print a machine-readable result line.")

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
    fetch.add_argument("--mouse-id", default=None, help="Mouse ID if no saved session state exists yet.")
    fetch.add_argument("--session-id", default=None, help="Session ID if no saved session state exists yet.")
    fetch.add_argument("--rsync-timeout-sec", type=float, default=CAMERA_RSYNC_TIMEOUT_SEC)
    fetch.add_argument("--ffmpeg-timeout-sec", type=float, default=CAMERA_FFMPEG_TIMEOUT_SEC)
    fetch.set_defaults(func=fetch_camera)

    convert = sub.add_parser("convert", parents=[common], help="Convert locally fetched .h264 files to MP4.")
    convert.add_argument("--mouse-id", default=None, help="Mouse ID if no saved session state exists yet.")
    convert.add_argument("--session-id", default=None, help="Session ID if no saved session state exists yet.")
    convert.add_argument("--ffmpeg-timeout-sec", type=float, default=CAMERA_FFMPEG_TIMEOUT_SEC)
    convert.set_defaults(func=convert_camera)

    preview = sub.add_parser("preview", parents=[common], help="Start a live camera preview, then stop it when you type y.")
    preview.set_defaults(func=preview_camera)

    stop_fetch = sub.add_parser("stop-fetch", parents=[common], help="Stop recording, then fetch files.")
    stop_fetch.add_argument("--mouse-id", default=None, help="Mouse ID if no saved session state exists yet.")
    stop_fetch.add_argument("--session-id", default=None, help="Session ID if no saved session state exists yet.")
    stop_fetch.add_argument("--remote-camera-stop", default=REMOTE_CAMERA_STOP)
    stop_fetch.add_argument("--ignore-stop-errors", action="store_true", default=False)
    stop_fetch.add_argument("--rsync-timeout-sec", type=float, default=CAMERA_RSYNC_TIMEOUT_SEC)
    stop_fetch.add_argument("--ffmpeg-timeout-sec", type=float, default=CAMERA_FFMPEG_TIMEOUT_SEC)

    def do_stop_fetch(args):
        try:
            state = load_state()
        except CameraStateNotFoundError:
            state = build_state_from_args(args)
        state = stop_camera(args, state)
        if not state.get("camera_stop_confirmed"):
            raise CameraStateError("Camera stop was not verified; refusing to fetch or delete remote camera files.")
        time.sleep(2.0)
        return fetch_camera(args, state)

    stop_fetch.set_defaults(func=do_stop_fetch)

    status = sub.add_parser("status", parents=[common], help="Check whether camera acquisition is running.")
    status.set_defaults(func=status_camera)

    last = sub.add_parser("last-state", help="Print last camera session state.")
    last.set_defaults(func=print_last_state)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    result = args.func(args)
    if getattr(args, "json", False) and result is not None:
        print(JSON_RESULT_PREFIX + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        raise
