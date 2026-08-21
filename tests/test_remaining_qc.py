import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# Stub out PIL so the Pi stimulus modules can be imported off-device.
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


def display_timing(duration_sec):
    return {
        "request_utc_iso": "2026-01-01T00:00:00.000000000+00:00",
        "request_unix_sec": "1767225600.000000000",
        "request_unix_ns": 1767225600000000000,
        "return_unix_ns": 1767225600516000000,
        "return_utc_iso": "2026-01-01T00:00:00.516000000+00:00",
        "request_perf_counter_ns": 10,
        "return_perf_counter_ns": 516000010,
        "duration_sec": duration_sec,
    }


class RemainingQcTests(unittest.TestCase):
    def test_conversion_defaults_to_long_timeout_without_faststart(self):
        self.assertEqual(rc.CAMERA_FFMPEG_TIMEOUT_SEC, 600.0)
        self.assertFalse(rc.CAMERA_MP4_FASTSTART)

        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            raw_path = directory / "session.h264"
            raw_path.write_bytes(b"raw-h264")
            captured = {}

            def fake_run(cmd, **kwargs):
                captured["cmd"] = cmd
                captured["kwargs"] = kwargs
                Path(cmd[-1]).write_bytes(b"valid-mp4")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch.object(rc.shutil, "which", return_value="/usr/bin/ffmpeg"), patch.object(
                rc.subprocess, "run", side_effect=fake_run
            ):
                result = rc.convert_h264_to_mp4(directory)

            self.assertTrue(result["conversion_completed"])
            self.assertNotIn("-movflags", captured["cmd"])
            self.assertIn("-c:v", captured["cmd"])
            self.assertEqual(captured["cmd"][captured["cmd"].index("-c:v") + 1], "copy")
            self.assertEqual(captured["kwargs"]["timeout"], 600.0)
            self.assertTrue(raw_path.exists())

    def test_nonzero_ffmpeg_return_preserves_raw_and_removes_partial_mp4(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            raw_path = directory / "session.h264"
            mp4_path = directory / "session.mp4"
            raw_path.write_bytes(b"raw-h264")

            def fake_run(cmd, **kwargs):
                mp4_path.write_bytes(b"partial")
                raise subprocess.CalledProcessError(7, cmd, output="stdout", stderr="ffmpeg failed")

            with patch.object(rc.shutil, "which", return_value="/usr/bin/ffmpeg"), patch.object(
                rc.subprocess, "run", side_effect=fake_run
            ):
                with self.assertRaises(rc.ConversionFailure) as raised:
                    rc.convert_h264_to_mp4(directory, timeout_sec=23.0)

            attempt = raised.exception.conversion_result["conversion_attempts"][0]
            self.assertEqual(attempt["reason"], "ffmpeg_nonzero_return")
            self.assertEqual(attempt["ffmpeg_return_code"], 7)
            self.assertTrue(attempt["raw_h264_still_present"])
            self.assertFalse(mp4_path.exists())
            self.assertTrue(raw_path.exists())

    def test_success_without_output_is_logged_as_invalid_output_and_preserves_raw(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            raw_path = directory / "session.h264"
            raw_path.write_bytes(b"raw-h264")

            with patch.object(rc.shutil, "which", return_value="/usr/bin/ffmpeg"), patch.object(
                rc.subprocess, "run", return_value=subprocess.CompletedProcess(["ffmpeg"], 0, stdout="", stderr="")
            ):
                with self.assertRaises(rc.ConversionFailure) as raised:
                    rc.convert_h264_to_mp4(directory)

            attempt = raised.exception.conversion_result["conversion_attempts"][0]
            self.assertEqual(attempt["reason"], "invalid_output")
            self.assertTrue(attempt["raw_h264_still_present"])
            self.assertTrue(raw_path.exists())

    def test_workflow_logs_conversion_attempt_details(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            raw_path = directory / "session.h264"
            raw_path.write_bytes(b"raw-h264")
            events = []

            def fake_run(cmd, **kwargs):
                Path(cmd[-1]).write_bytes(b"valid-mp4")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            with patch.object(rc.shutil, "which", return_value="/usr/bin/ffmpeg"), patch.object(
                rc.subprocess, "run", side_effect=fake_run
            ), patch.object(rc, "append_event", side_effect=lambda path, event, details=None: events.append((event, details or {}))):
                result = rc.run_camera_conversion_workflow("pi@host", directory, timeout_sec=41.0)

            self.assertTrue(result["camera_conversion_completed"])
            event_names = [event for event, _ in events]
            self.assertIn("camera_conversion_started", event_names)
            self.assertIn("camera_conversion_completed", event_names)
            started = next(details for event, details in events if event == "camera_conversion_started")
            completed = next(details for event, details in events if event == "camera_conversion_completed")
            self.assertEqual(started["input_h264_path"], str(raw_path))
            self.assertEqual(started["ffmpeg_timeout_sec"], 41.0)
            self.assertFalse(started["faststart_enabled"])
            self.assertTrue(completed["raw_h264_still_present"])
            self.assertGreater(completed["output_size_bytes"], 0)
            self.assertIn("conversion_elapsed_sec", completed)

    def test_workflow_logs_timeout_and_raw_preservation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            directory = Path(tmpdir)
            raw_path = directory / "session.h264"
            mp4_path = directory / "session.mp4"
            raw_path.write_bytes(b"raw-h264")
            events = []

            def fake_run(cmd, **kwargs):
                mp4_path.write_bytes(b"partial")
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

            with patch.object(rc.shutil, "which", return_value="/usr/bin/ffmpeg"), patch.object(
                rc.subprocess, "run", side_effect=fake_run
            ), patch.object(rc, "append_event", side_effect=lambda path, event, details=None: events.append((event, details or {}))):
                result = rc.run_camera_conversion_workflow("pi@host", directory, timeout_sec=600.0)

            self.assertFalse(result["camera_conversion_completed"])
            failed = next(details for event, details in events if event == "camera_conversion_failed")
            self.assertEqual(failed["reason"], "timeout")
            self.assertEqual(failed["ffmpeg_timeout_sec"], 600.0)
            self.assertTrue(failed["raw_h264_still_present"])
            self.assertFalse(mp4_path.exists())
            self.assertTrue(raw_path.exists())

    def test_display_timing_diagnostics_are_frame_based_and_non_compensating(self):
        trial = {
            "trial_index": 0,
            "repeat_number": 1,
            "image_index": 0,
            "image_id": 10,
            "image_filename": "image_010.png",
            "image_path": "/tmp/image_010.png",
            "planned_iti_frames": 51,
        }
        perf = SimpleNamespace(start_time=1.0, mean_interframe=2.0, stddev_interframe=3.0)

        stim_row = base.make_display_event_row("stim_on", trial, "/tmp/stim.raw", 0.5, perf, display_timing(0.516))
        iti_row = base.make_display_event_row("iti_on", trial, "/tmp/iti.raw", 51 / 60.0, perf, display_timing(0.867))

        self.assertEqual(stim_row["planned_display_frames"], 30)
        self.assertAlmostEqual(float(stim_row["display_call_overhead_sec"]), 0.016)
        self.assertAlmostEqual(float(stim_row["display_call_overhead_frames"]), 0.96)
        self.assertEqual(iti_row["planned_display_frames"], 51)
        self.assertAlmostEqual(float(iti_row["display_call_overhead_frames"]), (0.867 - 51 / 60.0) * 60.0)
        self.assertEqual(base.STIM_DURATION_SEC, 0.5)
        self.assertEqual(base.iti_frame_bounds(), (42, 60))


if __name__ == "__main__":
    unittest.main()
