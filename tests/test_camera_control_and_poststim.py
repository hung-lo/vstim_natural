import argparse
import subprocess
import sys
import tempfile
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

    def test_run_rsync_uses_partial_timeout_and_preserves_sources(self):
        captured = {}

        def fake_run_cmd(cmd, check=True, dry_run=False, timeout=None):
            captured["cmd"] = cmd
            captured["timeout"] = timeout
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        with patch.object(rc, "run_cmd", side_effect=fake_run_cmd):
            rc.run_rsync("pi@host", "/remote/session/video", Path("/tmp/local-video"), timeout_sec=123.0)

        self.assertIn("--partial", captured["cmd"])
        self.assertIn("--timeout=30", captured["cmd"])
        self.assertIn("--remove-source-files", captured["cmd"])
        self.assertEqual(captured["timeout"], 123.0)

    def test_convert_timeout_removes_incomplete_mp4_and_keeps_h264(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            h264_path = tmp_path / "session.h264"
            mp4_path = tmp_path / "session.mp4"
            h264_path.write_bytes(b"raw")

            def fake_run(cmd, check=True, text=True, capture_output=True, timeout=None):
                mp4_path.write_bytes(b"partial")
                raise subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=timeout)

            with patch.object(rc.shutil, "which", return_value="/usr/bin/ffmpeg"), patch.object(
                rc.subprocess,
                "run",
                side_effect=fake_run,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    rc.convert_h264_to_mp4(tmp_path, dry_run=False, timeout_sec=1.0)

            self.assertIn("timed out", str(ctx.exception).lower())
            self.assertTrue(h264_path.exists())
            self.assertFalse(mp4_path.exists())

    def test_fetch_timeout_logs_rsync_failure_without_fetch_return(self):
        events = []
        args = argparse.Namespace(dry_run=False, rsync_timeout_sec=1.0, ffmpeg_timeout_sec=1.0)
        state = {
            "camera_host": "pi@host",
            "remote_video_dir": "/remote/session/video",
            "local_video_dir": "/tmp/session/video",
            "framerate": 30,
        }

        def fake_append_event(local_video_dir, event, details=None):
            events.append((event, details or {}))

        with patch.object(rc, "run_rsync", side_effect=RuntimeError("Command timed out after 1.0 seconds")), patch.object(
            rc, "append_event", side_effect=fake_append_event
        ), patch("builtins.print"):
            with self.assertRaises(RuntimeError):
                rc.fetch_camera(args, state)

        event_names = [event for event, _ in events]
        self.assertIn("camera_fetch_requested", event_names)
        self.assertIn("camera_fetch_failed", event_names)
        self.assertNotIn("camera_fetch_returned", event_names)
        self.assertEqual(events[-1][1]["stage"], "rsync")

    def test_fetch_rejects_nonpositive_timeout_values(self):
        args = argparse.Namespace(dry_run=False, rsync_timeout_sec=0, ffmpeg_timeout_sec=1.0)
        state = {
            "camera_host": "pi@host",
            "remote_video_dir": "/remote/session/video",
            "local_video_dir": "/tmp/session/video",
            "framerate": 30,
        }
        with self.assertRaises(ValueError):
            rc.fetch_camera(args, state)

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
        ), patch.object(rc, "wait_for_remote_camera_ready", return_value={"camera_pid": 123, "stdout": "RUNNING:123", "returncode": 0}), patch.object(
            rc,
            "wait_for_remote_camera_output_growth",
            return_value={"camera_output_file": "/remote/session.h264", "initial_size_bytes": 100,
                          "confirmed_size_bytes": 500, "camera_output_file_detected": True,
                          "camera_output_growing_confirmed": True},
        ), patch("builtins.print"):
            state = rc.start_camera(args)

        self.assertEqual(calls[0], ("event", "camera_start_requested"))
        self.assertEqual(calls[1], ("save_state", "launch_requested"))
        self.assertEqual(calls[2][0], "run_ssh")
        self.assertEqual(state["camera_status"], "recording_confirmed")
        self.assertEqual(state["camera_pid"], 123)

        self.assertTrue(state["camera_pid_confirmed"])
        self.assertTrue(state["camera_output_growing_confirmed"])
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
            return {"camera_fetch_completed": True, "camera_conversion_completed": True, "camera_conversion_deferred": False}

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
            "camera_conversion_completed",
            "poststim_black_end",
            "session_end",
        ])
        self.assertTrue(result["camera_stop_confirmed"])
        self.assertTrue(result["camera_fetch_completed"])
        self.assertTrue(result["session_completed"])
        self.assertFalse(result["camera_fetch_deferred"])
        self.assertFalse(result["camera_left_running_by_user"])

    def test_failed_stop_is_retried_instead_of_falling_through(self):
        screen = FakeScreen()
        event_types = []
        prompt_results = [True, True]

        def fake_append_csv_row(path, row, fieldnames):
            event_types.append(row.get("event_type"))

        stop_calls = []
        fetch_calls = []

        def fake_stop_camera_recording():
            stop_calls.append("stop")
            if len(stop_calls) == 1:
                raise RuntimeError("stop failed")

        def fake_fetch_camera_recording():
            fetch_calls.append("fetch")
            return {"camera_fetch_completed": True, "camera_conversion_completed": True, "camera_conversion_deferred": False}

        with patch.object(base, "append_csv_row", side_effect=fake_append_csv_row), patch.object(base, "prompt_yes_no", side_effect=prompt_results), patch.object(
            cam, "stop_camera_recording", side_effect=fake_stop_camera_recording
        ), patch.object(cam, "fetch_camera_recording", side_effect=fake_fetch_camera_recording), patch.object(cam.time, "sleep", return_value=None), patch(
            "builtins.print"
        ):
            result = cam.run_poststim_black_baseline(screen, "iti_raw", Path("/tmp/event_log.csv"))

        self.assertEqual(stop_calls, ["stop", "stop"])
        self.assertEqual(fetch_calls, ["fetch"])
        self.assertTrue(result["camera_stop_confirmed"])
        self.assertFalse(result["camera_left_running_by_user"])
        self.assertTrue(result["camera_fetch_completed"])
        self.assertTrue(result["camera_conversion_completed"])
        self.assertFalse(result["camera_fetch_deferred"])

    def test_declining_retry_and_leave_running_keeps_prompting_until_stop_succeeds(self):
        screen = FakeScreen()
        event_types = []
        prompt_results = [True, False, False, True]

        def fake_append_csv_row(path, row, fieldnames):
            event_types.append(row.get("event_type"))

        stop_calls = []
        fetch_calls = []

        def fake_stop_camera_recording():
            stop_calls.append("stop")
            if len(stop_calls) == 1:
                raise RuntimeError("stop failed")

        def fake_fetch_camera_recording():
            fetch_calls.append("fetch")
            return {"camera_fetch_completed": True, "camera_conversion_completed": True, "camera_conversion_deferred": False}

        with patch.object(base, "append_csv_row", side_effect=fake_append_csv_row), patch.object(base, "prompt_yes_no", side_effect=prompt_results), patch.object(
            cam, "stop_camera_recording", side_effect=fake_stop_camera_recording
        ), patch.object(cam, "fetch_camera_recording", side_effect=fake_fetch_camera_recording), patch.object(cam.time, "sleep", return_value=None), patch(
            "builtins.print"
        ):
            result = cam.run_poststim_black_baseline(screen, "iti_raw", Path("/tmp/event_log.csv"))

        self.assertEqual(stop_calls, ["stop", "stop"])
        self.assertEqual(fetch_calls, ["fetch"])
        self.assertTrue(result["camera_stop_confirmed"])
        self.assertFalse(result["camera_left_running_by_user"])
        self.assertTrue(result["camera_fetch_completed"])
        self.assertTrue(result["camera_conversion_completed"])
        self.assertFalse(result["camera_fetch_deferred"])

    def test_keyboard_interrupt_during_fetch_marks_fetch_deferred(self):
        screen = FakeScreen()
        event_types = []

        def fake_append_csv_row(path, row, fieldnames):
            event_types.append(row.get("event_type"))

        stop_calls = []

        def fake_stop_camera_recording():
            stop_calls.append("stop")

        def fake_fetch_camera_recording():
            raise KeyboardInterrupt()

        with patch.object(base, "append_csv_row", side_effect=fake_append_csv_row), patch.object(base, "prompt_yes_no", side_effect=[True]), patch.object(
            cam, "stop_camera_recording", side_effect=fake_stop_camera_recording
        ), patch.object(cam, "fetch_camera_recording", side_effect=fake_fetch_camera_recording), patch.object(cam.time, "sleep", return_value=None), patch(
            "builtins.print"
        ):
            result = cam.run_poststim_black_baseline(screen, "iti_raw", Path("/tmp/event_log.csv"))

        self.assertEqual(stop_calls, ["stop"])
        self.assertTrue(result["camera_stop_confirmed"])
        self.assertFalse(result["camera_fetch_completed"])
        self.assertTrue(result["camera_fetch_deferred"])
        self.assertTrue(result["session_completed"])
        self.assertFalse(result["camera_left_running_by_user"])
        self.assertIn("camera_fetch_deferred", event_types)

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
            return {"camera_fetch_completed": True, "camera_conversion_completed": True, "camera_conversion_deferred": False}

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
        self.assertTrue(result["camera_fetch_completed"])
        self.assertTrue(result["camera_conversion_completed"])
        self.assertFalse(result["camera_fetch_deferred"])



    def test_keyboard_interrupt_at_fetch_retry_prompt_defers_once(self):
        screen = FakeScreen()
        events = []
        stop_calls = []
        fetch_calls = []

        def fake_append_csv_row(path, row, fieldnames):
            events.append(row)

        def fake_stop_camera_recording():
            stop_calls.append("stop")

        def fake_fetch_camera_recording():
            fetch_calls.append("fetch")
            raise RuntimeError("fetch failed")

        with patch.object(base, "append_csv_row", side_effect=fake_append_csv_row), patch.object(
            base, "prompt_yes_no", side_effect=[True, KeyboardInterrupt()]
        ), patch.object(cam, "stop_camera_recording", side_effect=fake_stop_camera_recording), patch.object(
            cam, "fetch_camera_recording", side_effect=fake_fetch_camera_recording
        ), patch.object(cam.time, "sleep", return_value=None), patch("builtins.print"):
            result = cam.run_poststim_black_baseline(screen, "iti_raw", Path("/tmp/event_log.csv"))

        event_types = [row["event_type"] for row in events]
        self.assertEqual(stop_calls, ["stop"])
        self.assertEqual(fetch_calls, ["fetch"])
        self.assertTrue(result["camera_stop_confirmed"])
        self.assertFalse(result["camera_fetch_completed"])
        self.assertTrue(result["camera_fetch_deferred"])
        self.assertTrue(result["session_completed"])
        self.assertEqual(event_types.count("camera_fetch_deferred"), 1)
        self.assertLess(event_types.index("camera_fetch_failed"), event_types.index("camera_fetch_deferred"))
        self.assertLess(event_types.index("camera_fetch_deferred"), event_types.index("poststim_black_end"))

    def test_start_wrapper_uses_emergency_timeout_and_structured_confirmation(self):
        captured = {}
        state = {
            "camera_pid_confirmed": True,
            "camera_output_growing_confirmed": True,
            "camera_output_file": "/remote/session.h264",
        }
        stdout = cam.CAMERA_CONTROL_RESULT_PREFIX + __import__("json").dumps(state) + "\n"

        def fake_run_camera_control(args, background=False, timeout=None):
            captured["args"] = args
            captured["timeout"] = timeout
            return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")

        with patch.object(cam, "run_camera_control", side_effect=fake_run_camera_control), patch("builtins.print"):
            result = cam.start_camera_recording("mouse", "session")

        self.assertGreaterEqual(captured["timeout"], 90.0)
        self.assertIn("--json", captured["args"])
        self.assertEqual(result["camera_output_file"], "/remote/session.h264")

    def test_start_failure_recovery_confirms_cleanup_without_starting(self):
        events = []
        stopped_state = {"camera_stop_confirmed": True, "camera_status": "stopped_confirmed"}

        with patch.object(cam, "start_camera_recording", side_effect=RuntimeError("start timeout")), patch.object(
            cam, "stop_camera_recording", return_value=stopped_state
        ) as stop_mock, patch.object(
            base, "append_csv_row", side_effect=lambda path, row, fields: events.append(row["event_type"])
        ), patch("builtins.print"):
            result = cam.start_camera_recording_with_recovery("mouse", "session", Path("/tmp/event.csv"))

        self.assertFalse(result["confirmed_running"])
        self.assertTrue(result["cleanup_attempted"])
        self.assertTrue(result["cleanup_confirmed"])
        self.assertFalse(result["camera_state_unknown_acknowledged"])
        stop_mock.assert_called_once_with()
        self.assertIn("camera_start_failed", events)
        self.assertIn("camera_cleanup_after_start_failure_confirmed", events)

    def test_output_growth_requires_same_file_to_increase(self):
        probes = iter(["", "/remote/session.h264\t100\n", "/remote/session.h264\t500\n"])

        def fake_run_ssh(camera_host, remote_cmd, check=True, dry_run=False, timeout=None):
            if remote_cmd.startswith("find "):
                return subprocess.CompletedProcess(["ssh"], 0, stdout=next(probes), stderr="")
            return subprocess.CompletedProcess(["ssh"], 0, stdout="RUNNING:123\n", stderr="")

        with patch.object(rc, "run_ssh", side_effect=fake_run_ssh), patch.object(rc.time, "sleep", return_value=None):
            result = rc.wait_for_remote_camera_output_growth(
                "pi@host", "/remote/camera.pid", "/remote", "/remote/camera.log", timeout_sec=1.0
            )

        self.assertTrue(result["camera_output_file_detected"])
        self.assertTrue(result["camera_output_growing_confirmed"])
        self.assertEqual(result["initial_size_bytes"], 100)
        self.assertEqual(result["confirmed_size_bytes"], 500)

    def test_static_output_is_not_ready(self):
        clock = iter([0.0, 0.0, 2.0])

        def fake_run_ssh(camera_host, remote_cmd, check=True, dry_run=False, timeout=None):
            if remote_cmd.startswith("find "):
                return subprocess.CompletedProcess(["ssh"], 0, stdout="/remote/session.h264\t100\n", stderr="")
            return subprocess.CompletedProcess(["ssh"], 0, stdout="RUNNING:123\n", stderr="")

        with patch.object(rc, "run_ssh", side_effect=fake_run_ssh), patch.object(
            rc.time, "monotonic", side_effect=lambda: next(clock)
        ), patch.object(rc.time, "sleep", return_value=None), patch.object(rc, "tail_remote_log", return_value="camera log"):
            with self.assertRaisesRegex(RuntimeError, "did not grow"):
                rc.wait_for_remote_camera_output_growth(
                    "pi@host", "/remote/camera.pid", "/remote", "/remote/camera.log", timeout_sec=1.0
                )

    def test_pid_death_aborts_output_readiness_immediately(self):
        with patch.object(
            rc,
            "run_ssh",
            return_value=subprocess.CompletedProcess(["ssh"], 1, stdout="NOT_RUNNING\n", stderr=""),
        ), patch.object(rc, "tail_remote_log", return_value="camera failed"):
            with self.assertRaisesRegex(RuntimeError, "process stopped"):
                rc.wait_for_remote_camera_output_growth(
                    "pi@host", "/remote/camera.pid", "/remote", "/remote/camera.log", timeout_sec=1.0
                )

    def test_stop_verification_polls_until_session_process_is_gone(self):
        results = iter([
            subprocess.CompletedProcess(["ssh"], 1, stdout="STILL_RUNNING\n", stderr=""),
            subprocess.CompletedProcess(["ssh"], 1, stdout="STILL_RUNNING\n", stderr=""),
            subprocess.CompletedProcess(["ssh"], 0, stdout="STOPPED\n", stderr=""),
        ])

        with patch.object(rc, "run_ssh", side_effect=lambda *args, **kwargs: next(results)) as run_mock, patch.object(
            rc.time, "sleep", return_value=None
        ):
            result = rc.wait_for_remote_camera_stopped(
                "pi@host", 123, "/remote/session", "/remote/camera.log", timeout_sec=1.0
            )

        self.assertTrue(result["camera_stop_confirmed"])
        self.assertEqual(run_mock.call_count, 3)

    def test_stop_camera_removes_pid_file_only_after_verification(self):
        args = argparse.Namespace(
            camera_host="pi@host", remote_camera_stop="/repo/stop.sh", ignore_stop_errors=False, dry_run=False
        )
        state = {
            "camera_host": "pi@host",
            "camera_pid": 123,
            "remote_pid_file": "/remote/camera.pid",
            "remote_base_path": "/remote/session",
            "remote_video_dir": "/remote",
            "remote_log_file": "/remote/camera.log",
            "local_video_dir": "/tmp/camera-stop-test",
        }
        commands = []

        def fake_run_ssh(camera_host, remote_cmd, check=True, dry_run=False, timeout=None):
            commands.append(remote_cmd)
            return subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")

        with patch.object(rc, "run_ssh", side_effect=fake_run_ssh), patch.object(
            rc, "wait_for_remote_camera_stopped", return_value={"camera_stop_confirmed": True}
        ) as verify_mock, patch.object(rc, "save_state"), patch.object(rc, "append_event"), patch("builtins.print"):
            result = rc.stop_camera(args, state)

        self.assertTrue(result["camera_stop_confirmed"])
        self.assertEqual(result["camera_status"], "stopped_confirmed")
        verify_mock.assert_called_once()
        self.assertNotIn("rm -f", commands[0])
        self.assertTrue(commands[-1].startswith("rm -f "))

    def test_ignore_stop_errors_never_creates_false_confirmation(self):
        args = argparse.Namespace(
            camera_host="pi@host", remote_camera_stop="/repo/stop.sh", ignore_stop_errors=True, dry_run=False
        )
        state = {
            "camera_host": "pi@host",
            "camera_pid": 123,
            "remote_pid_file": "/remote/camera.pid",
            "remote_base_path": "/remote/session",
            "remote_video_dir": "/remote",
            "remote_log_file": "/remote/camera.log",
            "local_video_dir": "/tmp/camera-stop-ignore-test",
        }

        with patch.object(
            rc, "run_ssh", return_value=subprocess.CompletedProcess(["ssh"], 0, stdout="", stderr="")
        ), patch.object(
            rc, "wait_for_remote_camera_stopped", side_effect=RuntimeError("still running")
        ), patch.object(rc, "save_state"), patch.object(rc, "append_event"), patch("builtins.print"):
            result = rc.stop_camera(args, state)

        self.assertEqual(result["camera_status"], "stop_unverified")
        self.assertFalse(result["camera_stop_confirmed"])

    def test_stopped_check_is_scoped_to_expected_pid_and_session_path(self):
        command = rc.build_remote_camera_stopped_check_command(123, "/remote/session/video/session")
        self.assertIn("kill -0", command)
        self.assertIn("123", command)
        self.assertIn("/remote/session/video/session", command)
        self.assertIn("grep -F", command)
if __name__ == "__main__":
    unittest.main()
