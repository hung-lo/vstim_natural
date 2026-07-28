import argparse
import subprocess
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Stub out PIL so run_stringer_vstim can import in environments without Pillow.
pil = types.ModuleType("PIL")
image = types.ModuleType("PIL.Image")
imagedraw = types.ModuleType("PIL.ImageDraw")
imageops = types.ModuleType("PIL.ImageOps")
pil.Image = image
pil.ImageDraw = imagedraw
pil.ImageOps = imageops
sys.modules.setdefault("PIL", pil)
sys.modules.setdefault("PIL.Image", image)
sys.modules.setdefault("PIL.ImageDraw", imagedraw)
sys.modules.setdefault("PIL.ImageOps", imageops)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import remote_camera_control as rc
import run_stringer_vstim as base
import run_stringer_vstim_cam as cam


class FakeScreen:
    def __init__(self):
        self.calls = []

    def display_raw(self, raw):
        self.calls.append(("display_raw", raw))

    def display_greyscale(self, level):
        self.calls.append(("display_greyscale", level))


class CameraControlAndPoststimTests(unittest.TestCase):
    def test_run_ssh_includes_hardening_options(self):
        captured = {}

        def fake_run_cmd(cmd, check=True, dry_run=False, timeout=None):
            captured["cmd"] = cmd
            captured["timeout"] = timeout
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(rc, "run_cmd", side_effect=fake_run_cmd):
            rc.run_ssh("pi@host", "echo hi", timeout=7.5)

        self.assertEqual(captured["cmd"][0], "ssh")
        self.assertIn("-n", captured["cmd"])
        self.assertIn("-T", captured["cmd"])
        self.assertIn("BatchMode=yes", captured["cmd"])
        self.assertEqual(captured["timeout"], 7.5)

    def test_run_cmd_timeout_raises_clear_error(self):
        with patch.object(rc.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd=["ssh"], timeout=3.0)):
            with self.assertRaises(RuntimeError) as ctx:
                rc.run_cmd(["ssh", "pi@host", "echo hi"], timeout=3.0)

        self.assertIn("timed out", str(ctx.exception).lower())

    def test_build_remote_launch_command_contains_detach_and_pid_file(self):
        paths = {
            "remote_video_dir": "/home/pi/stim logs/session/video",
            "remote_base_path": "/home/pi/stim logs/session/video/session",
        }
        cmd = rc.build_remote_camera_launch_command(
            paths,
            30,
            "/home/pi/RPi4_behavior_boxes",
            "/home/pi/RPi4_behavior_boxes/video_acquisition/start_acquisition.py",
            "/home/pi/stim logs/session/video/camera_acquisition.log",
            "/home/pi/stim logs/session/video/camera_acquisition.pid",
        )

        self.assertIn("</dev/null", cmd)
        self.assertIn("camera_acquisition.log", cmd)
        self.assertIn("camera_acquisition.pid", cmd)
        self.assertIn("CAMERA_PID=%s", cmd)

    def test_preview_launch_redirects_stdin(self):
        captured = {}

        def fake_run_ssh(camera_host, remote_cmd, check=True, dry_run=False, timeout=None):
            captured["remote_cmd"] = remote_cmd
            return subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")

        args = argparse.Namespace(camera_host="pi@host", dry_run=True)
        with patch.object(rc, "run_ssh", side_effect=fake_run_ssh), patch("builtins.input", return_value="y"):
            rc.preview_camera(args)

        self.assertIn("</dev/null", captured["remote_cmd"])
        self.assertIn("camera_preview.pid", captured["remote_cmd"])

    def test_start_camera_saves_state_before_launch_and_confirms_ready(self):
        calls = []
        args = argparse.Namespace(
            mouse_id="mouse",
            session_id="session",
            framerate=30,
            remote_camera_repo="/repo",
            remote_camera_start="/repo/start_acquisition.py",
            remote_camera_stop="/repo/stop_acquisition.sh",
            camera_host="pi@host",
            dry_run=False,
        )
        paths = {
            "mouse_id": "mouse",
            "session_id": "session",
            "local_session_dir": "/tmp/session",
            "local_video_dir": "/tmp/session/video",
            "remote_session_dir": "/home/pi/stim_logs/session",
            "remote_video_dir": "/home/pi/stim_logs/session/video",
            "remote_base_path": "/home/pi/stim_logs/session/video/session",
            "remote_log_file": "/home/pi/stim_logs/session/video/camera_acquisition.log",
            "remote_pid_file": "/home/pi/stim_logs/session/video/camera_acquisition.pid",
        }

        def record_save(state):
            calls.append(("save_state", state["camera_status"]))

        def record_event(local_video_dir, event, details=None):
            calls.append(("event", event))

        def fake_run_ssh(camera_host, remote_cmd, check=True, dry_run=False, timeout=None):
            calls.append(("run_ssh", remote_cmd))
            return subprocess.CompletedProcess(["ssh"], 0, stdout="CAMERA_PID=123\n", stderr="")

        with patch.object(rc, "make_session_paths", return_value=paths), patch.object(
            rc, "resolve_camera_host", return_value="pi@host"
        ), patch.object(rc, "save_state", side_effect=record_save), patch.object(rc, "append_event", side_effect=record_event), patch.object(
            rc, "run_ssh", side_effect=fake_run_ssh
        ), patch.object(rc, "wait_for_remote_camera_ready", return_value={"camera_pid": 123, "stdout": "RUNNING:123", "returncode": 0}), patch("builtins.print"):
            state = rc.start_camera(args)

        self.assertEqual(calls[0], ("event", "camera_start_requested"))
        self.assertEqual(calls[1], ("save_state", "launch_requested"))
        self.assertEqual(calls[2][0], "run_ssh")
        self.assertEqual(state["camera_status"], "recording_confirmed")
        self.assertEqual(state["camera_pid"], 123)

    def test_run_trial_sequence_can_skip_final_iti_for_camera_poststim(self):
        screen = FakeScreen()
        trials = [
            {"trial_index": 1, "repeat_number": 1, "image_index": 0, "image_id": "img1", "image_filename": "img1.png", "image_path": "/tmp/img1.png", "planned_stim_duration_sec": 0.5, "planned_iti_duration_sec": 0.75},
            {"trial_index": 2, "repeat_number": 1, "image_index": 1, "image_id": "img2", "image_filename": "img2.png", "image_path": "/tmp/img2.png", "planned_stim_duration_sec": 0.5, "planned_iti_duration_sec": 0.75},
        ]
        loaded_stim_raws = {"img1": "stim1", "img2": "stim2"}
        stim_raw_paths = {"img1": Path("/stim1.raw"), "img2": Path("/stim2.raw")}
        iti_raw_path = Path("/iti.raw")
        event_types = []

        def fake_display_raw_with_timing(screen_arg, raw):
            screen_arg.display_raw(raw)
            return (
                types.SimpleNamespace(start_time=1.0, mean_interframe=2.0, stddev_interframe=3.0),
                {
                    "request_utc_iso": "now",
                    "request_unix_sec": 1.0,
                    "return_utc_iso": "later",
                    "request_unix_ns": 10,
                    "return_unix_ns": 20,
                    "request_perf_counter_ns": 30,
                    "return_perf_counter_ns": 40,
                    "duration_sec": 0.5,
                },
            )

        def fake_append_csv_row(path, row, fieldnames):
            event_types.append(row["event_type"])

        with patch.object(base, "display_raw_with_timing", side_effect=fake_display_raw_with_timing), patch.object(base, "append_csv_row", side_effect=fake_append_csv_row), patch(
            "builtins.print"
        ):
            base.run_trial_sequence(
                screen,
                trials,
                loaded_stim_raws,
                "iti_raw",
                stim_raw_paths,
                iti_raw_path,
                Path("/tmp/event.csv"),
                include_final_iti=False,
            )

        self.assertEqual(screen.calls, [("display_raw", "stim1"), ("display_raw", "iti_raw"), ("display_raw", "stim2")])
        self.assertEqual(event_types, ["stim_on", "iti_on", "stim_on"])

    def test_black_poststim_helper_keeps_screen_black_during_stop_and_fetch(self):
        screen = FakeScreen()
        event_types = []
        prompt_results = [True]

        def fake_append_csv_row(path, row, fieldnames):
            event_types.append(row.get("event_type"))

        stop_calls = []
        fetch_calls = []

        def fake_stop_camera_recording():
            stop_calls.append("stop")

        def fake_fetch_camera_recording():
            fetch_calls.append("fetch")

        with patch.object(base, "append_csv_row", side_effect=fake_append_csv_row), patch.object(base, "prompt_yes_no", side_effect=prompt_results), patch.object(
            cam, "stop_camera_recording", side_effect=fake_stop_camera_recording
        ), patch.object(cam, "fetch_camera_recording", side_effect=fake_fetch_camera_recording), patch.object(cam.time, "sleep", return_value=None), patch(
            "builtins.print"
        ):
            result = cam.run_poststim_black_baseline(screen, "iti_raw", Path("/tmp/event_log.csv"))

        self.assertEqual(screen.calls[:4], [("display_raw", "iti_raw"), ("display_raw", "iti_raw"), ("display_raw", "iti_raw"), ("display_raw", "iti_raw")])
        self.assertEqual(screen.calls[4], ("display_greyscale", base.POSTSTIM_BLACK_LEVEL))
        self.assertEqual(stop_calls, ["stop"])
        self.assertEqual(fetch_calls, ["fetch"])
        self.assertEqual(event_types, [
            "poststim_gray_start",
            "poststim_gray_end",
            "poststim_black_on",
            "camera_stop_requested",
            "camera_stop_confirmed",
            "camera_fetch_started",
            "camera_fetch_completed",
            "poststim_black_end",
            "session_end",
        ])
        self.assertTrue(result["camera_stop_confirmed"])
        self.assertTrue(result["camera_fetch_completed"])
        self.assertTrue(result["session_completed"])
        self.assertFalse(result["camera_fetch_deferred"])
        self.assertFalse(result["camera_left_running_by_user"])

    def test_fetch_retry_does_not_call_stop_again(self):
        screen = FakeScreen()
        event_types = []
        prompt_results = [True, True]

        def fake_append_csv_row(path, row, fieldnames):
            event_types.append(row.get("event_type"))

        stop_calls = []
        fetch_calls = []

        def fake_stop_camera_recording():
            stop_calls.append("stop")

        def fake_fetch_camera_recording():
            fetch_calls.append("fetch")
            if len(fetch_calls) == 1:
                raise RuntimeError("fetch failed")

        with patch.object(base, "append_csv_row", side_effect=fake_append_csv_row), patch.object(base, "prompt_yes_no", side_effect=prompt_results), patch.object(
            cam, "stop_camera_recording", side_effect=fake_stop_camera_recording
        ), patch.object(cam, "fetch_camera_recording", side_effect=fake_fetch_camera_recording), patch.object(cam.time, "sleep", return_value=None), patch(
            "builtins.print"
        ):
            result = cam.run_poststim_black_baseline(screen, "iti_raw", Path("/tmp/event_log.csv"))

        self.assertEqual(stop_calls, ["stop"])
        self.assertEqual(fetch_calls, ["fetch", "fetch"])
        self.assertEqual(event_types.count("camera_fetch_started"), 2)
        self.assertIn("camera_fetch_failed", event_types)
        self.assertFalse(result["camera_fetch_deferred"])
        self.assertTrue(result["camera_fetch_completed"])


if __name__ == "__main__":
    unittest.main()
