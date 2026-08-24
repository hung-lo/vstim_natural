import io
import json
import math
import sys
import types
import unittest
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

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

import run_stringer_vstim as base
import run_stringer_vstim_cam as cam


class TtyBuffer(io.StringIO):
    def __init__(self, is_tty):
        super().__init__()
        self._is_tty = is_tty

    def isatty(self):
        return self._is_tty


class Patch2WorkflowTests(unittest.TestCase):
    def _run_mocked_camera_main_for_exit_path(self, primary_failure=False):
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            image_dir = root / "images"
            image_dir.mkdir()
            image_path = image_dir / "image_001.png"
            image_path.write_text("placeholder")
            output_root = root / "output"

            class FakeScreen:
                def __enter__(self):
                    return self

                def __exit__(self, exc_type, exc_value, traceback):
                    return False

                def load_raw(self, path):
                    return "raw:%s" % path

            fake_rpg = types.SimpleNamespace(Screen=lambda *args, **kwargs: FakeScreen())
            trials = [
                {
                    "trial_index": 0,
                    "image_index": 0,
                    "image_id": 1,
                    "image_filename": image_path.name,
                    "image_path": str(image_path),
                    "repeat_number": 0,
                    "planned_stim_duration_sec": 0.5,
                    "planned_iti_frames": 42,
                    "planned_iti_duration_sec": 0.7,
                    "iti_will_play": False,
                }
            ]
            display_timing = {
                "request_unix_ns": 1,
                "request_unix_sec": 1,
                "request_utc_iso": "2020-01-01T00:00:00Z",
                "return_unix_ns": 2,
                "return_utc_iso": "2020-01-01T00:00:00Z",
                "request_perf_counter_ns": 1,
                "return_perf_counter_ns": 2,
                "duration_sec": 0.1,
            }
            timestamp = {"utc_iso": "2020-01-01T00:00:00Z", "unix_sec": 1, "unix_ns": 1}
            prestim_result = {
                "requested_sec": 0.0,
                "minimum_gray_sec": 0.0,
                "camera_baseline_elapsed_sec": 0.0,
                "gray_elapsed_sec": 0.0,
                "forced": False,
                "end_reason": "timer_satisfied_during_preparation",
                "waited_for_minimum_gray_after_override": False,
                "remaining_camera_baseline_sec_at_gate_entry": 0.0,
            }
            poststim_result = defaultdict(lambda: False, {
                "stimulus_playback_completed": True,
                "camera_stop_confirmed": False,
                "camera_left_running_by_user": False,
                "camera_fetch_completed": False,
                "session_completed": False,
                "camera_cleanup_error": "",
                "remote_raw_cleanup_error": "",
                "camera_data_secured": False,
            })
            camera_start_result = {
                "confirmed_running": True,
                "controller_state": {
                    "camera_start_command_returned": True,
                    "camera_process_confirmed": True,
                    "camera_output_file_detected": True,
                    "camera_output_growing_confirmed": True,
                    "camera_output_file": "/remote/session/video/session.h264",
                    "camera_output_growth_confirmed_utc": "2020-01-01T00:00:00Z",
                },
                "error": "",
                "cleanup_attempted": False,
                "cleanup_confirmed": False,
                "camera_state_unknown_acknowledged": False,
            }

            with ExitStack() as stack:
                stack.enter_context(patch.dict(sys.modules, {"rpg": fake_rpg}))
                stack.enter_context(patch.object(cam, "OUTPUT_ROOT", output_root))
                stack.enter_context(patch.object(base, "resolve_image_dir", return_value=image_dir))
                stack.enter_context(patch.object(base, "list_png_files", return_value=[image_path]))
                stack.enter_context(patch.object(base, "select_image_subset", return_value=[image_path]))
                stack.enter_context(patch.object(base, "make_trial_sequence", return_value=(trials, 1, 2)))
                stack.enter_context(patch.object(base, "set_iti_playback_flags"))
                stack.enter_context(patch.object(base, "build_stim_raw_cache", return_value={"image_001": image_path}))
                stack.enter_context(patch.object(base, "build_iti_raw_cache", return_value={60: image_path}))
                stack.enter_context(patch.object(base, "display_raw_with_timing", return_value=(object(), display_timing)))
                stack.enter_context(patch.object(base, "capture_timestamp", return_value=timestamp))
                stack.enter_context(patch.object(base, "run_trial_sequence"))
                stack.enter_context(patch.object(base, "make_session_name", return_value="mouse_20200101T000000Z_vstim_natural"))
                stack.enter_context(patch.object(base, "utc_session_stamp", return_value="20200101T000000Z"))
                stack.enter_context(patch.object(base, "prompt_yes_no_strict", return_value=True))
                stack.enter_context(patch.object(base, "prompt_yes_no", return_value=True))
                stack.enter_context(patch.object(cam, "start_camera_recording_with_recovery", return_value=camera_start_result))
                stack.enter_context(patch.object(cam, "start_prestimulus_early_start_monitor", return_value=(None, None, None, False)))
                stack.enter_context(patch.object(cam, "stop_prestimulus_early_start_monitor"))
                stack.enter_context(patch.object(cam, "wait_for_prestimulus_gate", return_value=prestim_result))
                stop_error = RuntimeError("secondary cleanup failure" if primary_failure else "stop failed")
                stack.enter_context(patch.object(cam, "stop_camera_recording", side_effect=stop_error))
                if primary_failure:
                    stack.enter_context(
                        patch.object(cam, "run_poststim_black_baseline", side_effect=RuntimeError("primary failure"))
                    )
                else:
                    stack.enter_context(patch.object(cam, "run_poststim_black_baseline", return_value=poststim_result))

                raised = None
                result = None
                try:
                    result = cam.main(
                        [
                            "--camera",
                            "--mouse-id",
                            "mouse",
                            "--session-notes",
                            "test",
                            "--num-images",
                            "1",
                            "--repeats",
                            "1",
                            "--prestim-baseline-minutes",
                            "0",
                        ]
                    )
                except Exception as exc:
                    raised = exc

            session_root = output_root / "mouse_20200101T000000Z_vstim_natural"
            metadata = json.loads(next(session_root.glob("*_metadata.json")).read_text())
            manifest = json.loads((session_root / "session_manifest.json").read_text())
            return result, raised, metadata, manifest

    def test_required_mouse_id_reprompts_and_rejects_sanitized_empty(self):
        with patch.object(base, "prompt_text", side_effect=["   ", "!!!", "Mouse 7"]):
            self.assertEqual(base.prompt_required_text("Mouse: ", base.sanitize_text), "Mouse_7")

    def test_strict_numeric_prompts_reject_invalid_values(self):
        with patch.object(base, "prompt_text", side_effect=["2.9", "abc", "-1", "3"]):
            self.assertEqual(base.prompt_int_with_default("Trials", 2, minimum=1), 3)
        with patch.object(base, "prompt_text", side_effect=["nan", "inf", "-inf", "0.75"]):
            self.assertAlmostEqual(base.prompt_float_with_default("ITI", 1.0, minimum=0.0), 0.75)

    def test_strict_yes_no_reprompts_and_honors_only_explicit_default_blank(self):
        with patch.object(base, "prompt_text", side_effect=["maybe", "YES"]):
            self.assertTrue(base.prompt_yes_no_strict("Continue"))
        with patch.object(base, "prompt_text", return_value=""):
            self.assertTrue(base.prompt_yes_no_strict("Continue", default=True))
        with patch.object(base, "prompt_text", side_effect=["", "no"]):
            self.assertFalse(base.prompt_yes_no_strict("Continue"))

    def test_strict_prompts_raise_on_eof(self):
        with patch.object(base, "prompt_text", side_effect=RuntimeError("EOF")):
            with self.assertRaises(RuntimeError):
                base.prompt_required_text("Mouse: ")
            with self.assertRaises(RuntimeError):
                base.prompt_int_with_default("Trials", 2, minimum=1)
            with self.assertRaises(RuntimeError):
                base.prompt_float_with_default("ITI", 1.0, minimum=0.0)
            with self.assertRaises(RuntimeError):
                base.prompt_yes_no_strict("Continue", default=True)

    def test_outer_camera_stop_failure_is_nonzero_and_persisted(self):
        result, raised, metadata, manifest = self._run_mocked_camera_main_for_exit_path()
        self.assertIsNone(raised)
        self.assertEqual(result, 2)
        self.assertEqual(metadata["camera_cleanup_error"], "stop failed")
        self.assertEqual(metadata["session_status"], "protocol_complete_camera_cleanup_failed")
        self.assertEqual(manifest["status"], metadata["session_status"])
        self.assertNotEqual(result, 0)
        self.assertNotEqual(metadata["session_status"], "complete")

    def test_primary_failure_remains_primary_when_camera_cleanup_also_fails(self):
        result, raised, metadata, manifest = self._run_mocked_camera_main_for_exit_path(primary_failure=True)
        self.assertIsNone(result)
        self.assertIsNotNone(raised)
        self.assertEqual(str(raised), "primary failure")
        self.assertEqual(metadata["camera_cleanup_error"], "secondary cleanup failure")
        self.assertEqual(metadata["session_status"], "failed")
        self.assertEqual(manifest["status"], "failed")

    def test_camera_selection_modes(self):
        self.assertTrue(cam.resolve_camera_selection(True, True))
        self.assertFalse(cam.resolve_camera_selection(False, True))
        with self.assertRaises(RuntimeError):
            cam.resolve_camera_selection(True, False)
        with patch.object(base, "prompt_yes_no_strict", return_value=False) as prompt_mock:
            self.assertFalse(cam.resolve_camera_selection(None, True))
        prompt_mock.assert_called_once()
        with patch.object(base, "prompt_yes_no_strict") as prompt_mock:
            self.assertFalse(cam.resolve_camera_selection(None, False))
        prompt_mock.assert_not_called()

    def test_exact_planned_eta_uses_realized_itis(self):
        sequence = [
            {"planned_stim_duration_sec": 0.5, "planned_iti_duration_sec": 0.7, "iti_will_play": True},
            {"planned_stim_duration_sec": 0.5, "planned_iti_duration_sec": 1.0, "iti_will_play": True},
            {"planned_stim_duration_sec": 0.5, "planned_iti_duration_sec": 0.9, "iti_will_play": False},
        ]
        self.assertAlmostEqual(base.planned_task_duration_seconds(sequence), 3.2)
        self.assertAlmostEqual(base.planned_task_remaining_seconds(sequence, 0), 3.2)
        self.assertAlmostEqual(base.planned_task_remaining_seconds(sequence, 1), 2.0)
        self.assertAlmostEqual(base.planned_task_remaining_during_iti(sequence, 0, 0.2), 2.2)
        self.assertEqual(base.planned_task_remaining_seconds(sequence, 3), 0.0)

    def test_status_reporter_throttles_tty_and_phase_transitions(self):
        stream = TtyBuffer(True)
        reporter = base.StatusReporter(camera_enabled=False, stream=stream)
        reporter.emit("first", "TASK", now=10.0)
        reporter.emit("suppressed", "TASK", now=10.5)
        reporter.emit("phase", "BLACK BASELINE", now=10.5)
        reporter.emit("second", "BLACK BASELINE", now=11.6)
        output = stream.getvalue()
        self.assertIn("first", output)
        self.assertNotIn("suppressed", output)
        self.assertIn("phase", output)
        self.assertIn("second", output)
        self.assertIn("\r", output)

    def test_status_reporter_non_tty_uses_long_heartbeat(self):
        stream = TtyBuffer(False)
        reporter = base.StatusReporter(camera_enabled=False, stream=stream)
        reporter.emit("first", "TASK", now=10.0)
        reporter.emit("suppressed", "TASK", now=20.0)
        reporter.emit("second", "TASK", now=40.1)
        self.assertEqual(stream.getvalue().splitlines(), ["first", "second"])

    def test_status_reporter_formats_camera_and_black_baseline_states(self):
        stream = TtyBuffer(False)
        reporter = base.StatusReporter(camera_enabled=True, stream=stream)
        reporter.camera_confirmed(100_000_000_000)
        reporter.task(1, 2, [
            {"planned_stim_duration_sec": 1.0, "planned_iti_duration_sec": 0.5, "iti_will_play": True},
            {"planned_stim_duration_sec": 1.0, "planned_iti_duration_sec": 0.5, "iti_will_play": False},
        ], force=True)
        reporter.black_baseline(12.0, force=True)
        reporter.camera_stop_confirmed(160_000_000_000)
        reporter.phase("CAMERA STOPPED", "REC stopped 00:00:00 | camera stop confirmed")
        output = stream.getvalue()
        self.assertIn("REC", output)
        self.assertIn("TASK 1/2", output)
        self.assertIn("BLACK BASELINE", output)
        self.assertIn("camera stop confirmed", output)

    def test_camera_elapsed_freezes_at_stop(self):
        reporter = base.StatusReporter(camera_enabled=True, stream=TtyBuffer(False))
        reporter.camera_confirmed(100_000_000_000)
        self.assertAlmostEqual(reporter.camera_elapsed_seconds(120_000_000_000), 20.0)
        reporter.camera_stop_confirmed(160_000_000_000)
        self.assertAlmostEqual(reporter.camera_elapsed_seconds(220_000_000_000), 60.0)

    def test_session_status_table(self):
        self.assertEqual(base.derive_session_status(True), "complete")
        self.assertEqual(
            base.derive_session_status(True, True, True, False), "protocol_complete_video_pending"
        )
        self.assertEqual(
            base.derive_session_status(True, True, False, False, True),
            "stimulus_complete_camera_left_running",
        )
        self.assertEqual(base.derive_session_status(False, primary_failure=True), "failed")
        self.assertEqual(
            base.derive_session_status(True, camera_enabled=True, camera_cleanup_error="oops", primary_failure=True),
            "failed",
        )
        self.assertEqual(base.derive_session_status(False, interrupted=True), "interrupted")
        self.assertEqual(base.derive_session_status(True, noncamera_cleanup_error="oops"), "cleanup_failed")
        self.assertEqual(
            base.derive_session_status(
                True,
                camera_enabled=True,
                camera_stop_confirmed=True,
                camera_data_secured=False,
                camera_cleanup_error="cleanup failed",
            ),
            "protocol_complete_camera_cleanup_failed",
        )
        self.assertEqual(
            base.derive_session_status(
                True,
                camera_enabled=True,
                camera_stop_confirmed=True,
                camera_data_secured=True,
            ),
            "complete",
        )
        self.assertEqual(cam.finalize_exit_code(0, "protocol_complete_camera_cleanup_failed"), 2)
        self.assertEqual(cam.finalize_exit_code(0, "stimulus_complete_camera_left_running"), 0)

    def test_black_baseline_operator_commands(self):
        class FakeStdin:
            def __init__(self, value):
                self.value = value

            def readline(self):
                return self.value

        for value, expected in [
            ("\n", "stop"),
            ("y\n", "stop"),
            ("yes\n", "stop"),
            ("s\n", "stop"),
            ("stop\n", "stop"),
            ("l\n", "leave"),
            ("leave\n", "leave"),
        ]:
            with patch.object(cam.sys, "stdin", FakeStdin(value)), patch.object(
                cam.select, "select", return_value=([cam.sys.stdin], [], [])
            ):
                self.assertEqual(cam.black_baseline_operator_command(0.0), expected)

        with patch.object(cam.sys, "stdin", FakeStdin("maybe\n")), patch.object(
            cam.select, "select", return_value=([cam.sys.stdin], [], [])
        ):
            self.assertIsNone(cam.black_baseline_operator_command(0.0))

        with patch.object(cam.sys, "stdin", FakeStdin("")), patch.object(
            cam.select, "select", return_value=([cam.sys.stdin], [], [])
        ):
            with self.assertRaises(RuntimeError):
                cam.black_baseline_operator_command(0.0)

    def test_black_baseline_live_status_is_serviced_while_waiting(self):
        class InteractiveStdin:
            def __init__(self):
                self.lines = ["y\n"]

            def isatty(self):
                return True

            def readline(self):
                return self.lines.pop(0)

        class FakeScreen:
            def display_raw(self, raw):
                pass

            def display_greyscale(self, level):
                pass

        stdin = InteractiveStdin()
        stream = TtyBuffer(True)
        reporter = base.StatusReporter(camera_enabled=True, stream=stream)
        monotonic_value = [0.0]

        def monotonic():
            monotonic_value[0] += 1.0
            return monotonic_value[0]

        with patch.object(cam.sys, "stdin", stdin), patch.object(
            cam.select, "select", side_effect=[([], [], []), ([], [], []), ([stdin], [], [])]
        ), patch.object(cam.time, "monotonic", side_effect=monotonic), patch.object(
            cam.time, "sleep", return_value=None
        ), patch.object(cam, "stop_camera_recording", return_value={}), patch.object(
            cam, "fetch_camera_recording", return_value={
                "camera_fetch_completed": True,
                "camera_conversion_completed": True,
                "camera_conversion_deferred": False,
            }
        ), patch.object(base, "append_csv_row"), patch("builtins.print"):
            result = cam.run_poststim_black_baseline(
                FakeScreen(),
                "iti_raw",
                Path("/tmp/event.csv"),
                status_reporter=reporter,
            )

        output = stream.getvalue()
        self.assertGreaterEqual(output.count("BLACK BASELINE"), 2)
        self.assertIn("00:00:01", output)
        self.assertTrue(result["camera_stop_confirmed"])

    def test_session_manifest_is_atomic_and_uses_relative_paths(self):
        with self.subTest("manifest"):
            with __import__("tempfile").TemporaryDirectory() as tmpdir:
                root = Path(tmpdir)
                metadata = {"session_id": "s", "mouse_id": "m", "session_status": "complete"}
                metadata_path = root / "s_metadata.json"
                metadata_path.write_text(json.dumps(metadata))
                manifest_path = base.write_session_manifest(
                    root, metadata, {"metadata": metadata_path, "event_log": root / "s_event_log.csv"}
                )
                payload = json.loads(manifest_path.read_text())
                self.assertEqual(payload["files"]["metadata"], "s_metadata.json")
                self.assertEqual(payload["status"], "complete")
                self.assertFalse(list(root.glob("*.tmp")))

    def test_manifest_status_matches_metadata_for_camera_outcomes(self):
        statuses = [
            "complete",
            "protocol_complete_video_pending",
            "protocol_complete_camera_cleanup_failed",
            "stimulus_complete_camera_left_running",
        ]
        with __import__("tempfile").TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for index, status in enumerate(statuses):
                session_root = root / ("session_%d" % index)
                session_root.mkdir()
                metadata = {
                    "session_id": "s%d" % index,
                    "mouse_id": "m",
                    "session_status": status,
                }
                metadata_path = session_root / "metadata.json"
                base.atomic_write_json(metadata_path, metadata)
                manifest_path = base.write_session_manifest(session_root, metadata, {"metadata": metadata_path})
                self.assertEqual(json.loads(metadata_path.read_text())["session_status"], status)
                self.assertEqual(json.loads(manifest_path.read_text())["status"], status)

    def test_operational_timing_fields_are_present_and_monotonic(self):
        fields = [
            "initial_gray_start_monotonic_ns",
            "initial_gray_end_monotonic_ns",
            "task_start_monotonic_ns",
            "task_end_monotonic_ns",
            "final_gray_start_monotonic_ns",
            "final_gray_end_monotonic_ns",
            "black_baseline_start_monotonic_ns",
            "black_baseline_end_monotonic_ns",
            "black_baseline_elapsed_sec",
            "camera_recording_request_monotonic_ns",
            "camera_recording_confirmed_monotonic_ns",
            "camera_stop_confirmed_monotonic_ns",
            "camera_recording_elapsed_local_sec",
        ]
        metadata = {field: index + 1 for index, field in enumerate(fields)}
        self.assertEqual(set(fields), set(metadata))
        monotonic_fields = [field for field in fields if field.endswith("_monotonic_ns")]
        values = [metadata[field] for field in monotonic_fields]
        self.assertEqual(values, sorted(values))


if __name__ == "__main__":
    unittest.main()
