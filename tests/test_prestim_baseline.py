import sys
import threading
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
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

import run_stringer_vstim as base
import run_stringer_vstim_cam as cam


class FakeClock:
    def __init__(self, now):
        self.now = now

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeGateEvent:
    def __init__(self, clock, scripted_returns=None):
        self.clock = clock
        self.scripted_returns = list(scripted_returns or [])
        self.wait_timeouts = []
        self._set = False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        if self.scripted_returns:
            result = self.scripted_returns.pop(0)
        else:
            result = False
        if result:
            self._set = True
        else:
            self.clock.advance(timeout)
        return result


class FakeStdin:
    def __init__(self, lines, tty=True):
        self._lines = list(lines)
        self._tty = tty

    def isatty(self):
        return self._tty

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        return ""


class FakeCanvas:
    pass


class FakePerf:
    def __init__(self):
        self.start_time = 123.0
        self.mean_interframe = 456.0
        self.stddev_interframe = 7.0


class FakeScreen:
    def __init__(self, call_order, iti_path, stim_paths):
        self.call_order = call_order
        self.iti_path = str(iti_path)
        self.stim_paths = {str(path): stem for stem, path in stim_paths.items()}
        self.display_calls = 0

    def __enter__(self):
        self.call_order.append("Screen.__enter__")
        return self

    def __exit__(self, exc_type, exc, tb):
        self.call_order.append("Screen.__exit__")
        return False

    def load_raw(self, path):
        if path == self.iti_path:
            self.call_order.append("load_iti_raw")
            return "iti_raw"
        if path in self.stim_paths:
            self.call_order.append("load_stim_raws")
            return "stim_raw:%s" % self.stim_paths[path]
        self.call_order.append("load_raw:%s" % Path(path).name)
        return "raw:%s" % Path(path).name

    def display_raw(self, raw):
        self.display_calls += 1
        if raw == "iti_raw" and self.display_calls == 1:
            self.call_order.append("display_baseline_raw")
        elif raw == "iti_raw":
            self.call_order.append("display_final_gray_raw")
        else:
            self.call_order.append("display_other_raw")
        return FakePerf()


class PrestimBaselineTests(unittest.TestCase):
    def test_prompt_float_or_default_accepts_valid_input_and_rejects_invalid(self):
        with patch.object(base, "prompt_text", return_value=""):
            self.assertEqual(cam.prompt_float_or_default("Baseline", 3.0), 3.0)

        with patch.object(base, "prompt_text", return_value="2.5"):
            self.assertEqual(cam.prompt_float_or_default("Baseline", 3.0), 2.5)

        with patch.object(base, "prompt_text", return_value="0"):
            self.assertEqual(cam.prompt_float_or_default("Baseline", 3.0), 0.0)

        for raw_value in ["-1", "nan", "inf", "-inf"]:
            with self.subTest(raw_value=raw_value), patch.object(base, "prompt_text", return_value=raw_value):
                with self.assertRaises(ValueError):
                    cam.prompt_float_or_default("Baseline", 3.0)

    def test_is_early_start_command(self):
        self.assertTrue(cam.is_early_start_command("y"))
        self.assertTrue(cam.is_early_start_command(" YES "))
        self.assertFalse(cam.is_early_start_command("n"))
        self.assertFalse(cam.is_early_start_command(""))

    def test_build_raw_cache_split(self):
        with TemporaryDirectory() as tmp_dir:
            session_raw_dir = Path(tmp_dir) / "raw_cache"
            img1 = Path("/tmp/img_001.png")
            img2 = Path("/tmp/img_002.png")
            convert_calls = []

            def fake_build_canvas(image_path, screen_size, photodiode_on):
                return FakeCanvas()

            def fake_convert_canvas_to_rpg_raw(rpg_module, canvas, raw_path, duration_sec):
                convert_calls.append((Path(raw_path).name, duration_sec))
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text("raw")
                return raw_path

            with patch.object(base, "build_canvas", side_effect=fake_build_canvas), patch.object(
                base, "convert_canvas_to_rpg_raw", side_effect=fake_convert_canvas_to_rpg_raw
            ):
                stim_paths = base.build_stim_raw_cache(object(), session_raw_dir, [img1, img2])
                iti_path = base.build_iti_raw_cache(object(), session_raw_dir)
                combined_stim_paths, combined_iti_path = base.build_session_raw_cache(
                    object(),
                    Path(tmp_dir) / "raw_cache_2",
                    [img1, img2],
                )

        self.assertEqual(set(stim_paths.keys()), {"img_001", "img_002"})
        self.assertEqual(set(iti_path), set(range(42, 61)))
        self.assertEqual(set(combined_iti_path), set(range(42, 61)))
        self.assertEqual(Path(iti_path[42]).name, "gray_iti_042f.raw")
        self.assertEqual(Path(combined_iti_path[60]).name, "gray_iti_060f.raw")
        self.assertEqual(set(combined_stim_paths.keys()), {"img_001", "img_002"})
        self.assertEqual([name for name, _ in convert_calls[:2]], ["img_001.raw", "img_002.raw"])
        self.assertEqual(convert_calls[2][0], "gray_iti_042f.raw")

    def test_wait_for_prestimulus_gate_timer_elapsed_and_gray_elapsed(self):
        clock = FakeClock(5.0)
        event = FakeGateEvent(clock)
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(0.0, 10.0, 0.0, 3.0, event)

        self.assertEqual(result["end_reason"], "timer_elapsed")
        self.assertFalse(result["forced"])
        self.assertEqual(result["remaining_camera_baseline_sec_at_gate_entry"], 5.0)
        self.assertEqual(result["remaining_minimum_gray_sec_at_gate_entry"], 0.0)
        self.assertAlmostEqual(result["camera_baseline_elapsed_sec"], 10.0)
        self.assertAlmostEqual(result["gray_elapsed_sec"], 10.0)
        self.assertEqual(event.wait_timeouts, [5.0])

    def test_wait_for_prestimulus_gate_status_countdown_updates_before_callback(self):
        clock = FakeClock(0.0)
        event = FakeGateEvent(clock)
        callbacks = []

        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(
                0.0,
                10.0,
                0.0,
                3.0,
                event,
                status_callback=lambda state: callbacks.append(
                    (state["camera_baseline_elapsed_sec"], state["requested_sec"] - state["camera_baseline_elapsed_sec"])
                ),
            )

        self.assertGreater(len(callbacks), 1)
        self.assertEqual([elapsed for elapsed, _ in callbacks], sorted(elapsed for elapsed, _ in callbacks))
        self.assertLess(callbacks[-1][1], callbacks[0][1])
        self.assertGreaterEqual(result["camera_baseline_elapsed_sec"], 10.0)

    def test_wait_for_prestimulus_gate_timer_elapsed_but_gray_not_ready(self):
        clock = FakeClock(5.0)
        event = FakeGateEvent(clock)
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(0.0, 10.0, 0.0, 15.0, event)

        self.assertEqual(result["end_reason"], "timer_elapsed")
        self.assertFalse(result["forced"])
        self.assertEqual(result["remaining_camera_baseline_sec_at_gate_entry"], 5.0)
        self.assertEqual(result["remaining_minimum_gray_sec_at_gate_entry"], 10.0)
        self.assertAlmostEqual(result["camera_baseline_elapsed_sec"], 15.0)
        self.assertAlmostEqual(result["gray_elapsed_sec"], 15.0)
        self.assertEqual(sum(event.wait_timeouts), 10.0)

    def test_wait_for_prestimulus_gate_timer_satisfied_during_preparation(self):
        clock = FakeClock(20.0)
        event = FakeGateEvent(clock)
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(0.0, 10.0, 0.0, 5.0, event)

        self.assertEqual(result["end_reason"], "timer_satisfied_during_preparation")
        self.assertFalse(result["forced"])
        self.assertEqual(result["remaining_camera_baseline_sec_at_gate_entry"], 0.0)
        self.assertEqual(result["remaining_minimum_gray_sec_at_gate_entry"], 0.0)
        self.assertAlmostEqual(result["camera_baseline_elapsed_sec"], 20.0)
        self.assertAlmostEqual(result["gray_elapsed_sec"], 20.0)
        self.assertEqual(event.wait_timeouts, [])

    def test_wait_for_prestimulus_gate_user_override_and_gray_elapsed(self):
        clock = FakeClock(5.0)
        event = FakeGateEvent(clock, scripted_returns=[True])
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(0.0, 10.0, 0.0, 3.0, event)

        self.assertEqual(result["end_reason"], "user_override")
        self.assertTrue(result["forced"])
        self.assertFalse(result["waited_for_minimum_gray_after_override"])
        self.assertEqual(result["remaining_camera_baseline_sec_at_gate_entry"], 5.0)
        self.assertEqual(event.wait_timeouts, [5.0])

    def test_wait_for_prestimulus_gate_user_override_but_gray_not_ready(self):
        clock = FakeClock(5.0)
        event = FakeGateEvent(clock)
        event.set()
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch.object(cam.time, "sleep", side_effect=clock.advance), patch(
            "builtins.print"
        ):
            result = cam.wait_for_prestimulus_gate(0.0, 10.0, 0.0, 15.0, event)

        self.assertEqual(result["end_reason"], "user_override")
        self.assertTrue(result["forced"])
        self.assertTrue(result["waited_for_minimum_gray_after_override"])
        self.assertEqual(result["remaining_camera_baseline_sec_at_gate_entry"], 5.0)
        self.assertEqual(event.wait_timeouts, [])
        self.assertAlmostEqual(result["camera_baseline_elapsed_sec"], 15.0)
        self.assertAlmostEqual(result["gray_elapsed_sec"], 15.0)

    def test_wait_for_prestimulus_gate_zero_baseline(self):
        clock = FakeClock(100.0)
        event = FakeGateEvent(clock)
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(100.0, 0.0, 90.0, 3.0, event)

        self.assertEqual(result["end_reason"], "timer_satisfied_during_preparation")
        self.assertFalse(result["forced"])
        self.assertEqual(result["remaining_camera_baseline_sec_at_gate_entry"], 0.0)
        self.assertEqual(event.wait_timeouts, [])

    def test_watch_for_early_start_sets_event_on_yes(self):
        force_event = threading.Event()
        stop_event = threading.Event()
        fake_stdin = FakeStdin(["y\n"], tty=True)

        with patch.object(cam.sys, "stdin", fake_stdin), patch.object(cam.select, "select", side_effect=[([fake_stdin], [], [])]), patch(
            "builtins.print"
        ):
            thread = threading.Thread(target=cam.watch_for_early_start, args=(force_event, stop_event), daemon=True)
            thread.start()
            thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(force_event.is_set())

    def test_watch_for_early_start_stops_when_requested(self):
        force_event = threading.Event()
        stop_event = threading.Event()
        fake_stdin = FakeStdin([], tty=True)
        with patch.object(cam.sys, "stdin", fake_stdin):
            thread = threading.Thread(target=cam.watch_for_early_start, args=(force_event, stop_event), daemon=True)
            stop_event.set()
            thread.start()
            thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertFalse(force_event.is_set())

    def test_gray_first_flow_order(self):
        call_order = []
        iti_path = Path("/tmp/fake/raw_cache/iti/gray_iti.raw")
        stim_paths = {
            "img_001": Path("/tmp/fake/raw_cache/stim_on/img_001.raw"),
            "img_002": Path("/tmp/fake/raw_cache/stim_on/img_002.raw"),
        }
        fake_trials = [
            {
                "trial_index": 0,
                "repeat_number": 1,
                "image_index": 0,
                "image_id": 1,
                "image_filename": "img_001.png",
                "image_path": "/tmp/img_001.png",
                "planned_stim_duration_sec": base.STIM_DURATION_SEC,
                "planned_iti_duration_sec": base.ITI_DURATION_SEC,
            }
        ]

        def fake_build_iti_raw_cache(rpg_module, session_raw_dir):
            call_order.append("build_iti_raw_cache")
            return iti_path

        def fake_build_stim_raw_cache(rpg_module, session_raw_dir, selected_image_files):
            call_order.append("build_stim_raw_cache")
            return stim_paths

        def fake_start_camera_recording(mouse_id, session_id):
            call_order.append("start_camera_recording")

        def fake_start_monitor():
            call_order.append("start_input_monitor")
            return threading.Event(), threading.Event(), None, True

        def fake_wait_for_gate(*args, **kwargs):
            call_order.append("wait_for_gate")
            return {
                "requested_sec": 10.0,
                "minimum_gray_sec": 3.0,
                "remaining_camera_baseline_sec_at_gate_entry": 0.0,
                "remaining_minimum_gray_sec_at_gate_entry": 0.0,
                "camera_baseline_elapsed_sec": 10.0,
                "gray_elapsed_sec": 10.0,
                "forced": False,
                "end_reason": "timer_satisfied_during_preparation",
                "waited_for_minimum_gray_after_override": False,
            }

        def fake_stop_monitor(stop_event, thread):
            call_order.append("stop_input_monitor")

        def fake_run_trial_sequence(*args, **kwargs):
            call_order.append("run_trial_sequence")

        screen = FakeScreen(call_order, iti_path, stim_paths)
        with patch.object(base, "build_iti_raw_cache", side_effect=fake_build_iti_raw_cache), patch.object(
            base, "build_stim_raw_cache", side_effect=fake_build_stim_raw_cache
        ), patch.object(cam, "start_camera_recording", side_effect=fake_start_camera_recording), patch.object(
            cam, "start_prestimulus_early_start_monitor", side_effect=fake_start_monitor
        ), patch.object(cam, "wait_for_prestimulus_gate", side_effect=fake_wait_for_gate), patch.object(
            cam, "stop_prestimulus_early_start_monitor", side_effect=fake_stop_monitor
        ), patch.object(base, "run_trial_sequence", side_effect=fake_run_trial_sequence), patch.object(
            base, "display_raw_with_timing", wraps=base.display_raw_with_timing
        ):
            call_order.append("before_build_iti")
            iti_raw_path = base.build_iti_raw_cache(object(), Path("/tmp/raw_cache"))
            with screen as active_screen:
                iti_raw = active_screen.load_raw(str(iti_raw_path))
                base.display_raw_with_timing(active_screen, iti_raw)
                cam.start_camera_recording("mouse", "session")
                cam.start_prestimulus_early_start_monitor()
                stim_raw_paths = base.build_stim_raw_cache(object(), Path("/tmp/raw_cache"), [Path("/tmp/img_001.png"), Path("/tmp/img_002.png")])
                for image_path in [Path("/tmp/img_001.png"), Path("/tmp/img_002.png")]:
                    active_screen.load_raw(str(stim_raw_paths[image_path.stem]))
                cam.wait_for_prestimulus_gate(0.0, 10.0, 0.0, 3.0, threading.Event())
                cam.stop_prestimulus_early_start_monitor(threading.Event(), None)
                base.run_trial_sequence(active_screen, fake_trials, {"img_001": "stim1"}, iti_raw, stim_paths, iti_path, Path("/tmp/event_log.csv"), gpio=None)

        self.assertLess(call_order.index("build_iti_raw_cache"), call_order.index("Screen.__enter__"))
        self.assertLess(call_order.index("load_iti_raw"), call_order.index("display_baseline_raw"))
        self.assertLess(call_order.index("display_baseline_raw"), call_order.index("start_camera_recording"))
        self.assertLess(call_order.index("start_camera_recording"), call_order.index("build_stim_raw_cache"))
        self.assertLess(call_order.index("build_stim_raw_cache"), call_order.index("load_stim_raws"))
        self.assertLess(call_order.index("load_stim_raws"), call_order.index("wait_for_gate"))
        self.assertLess(call_order.index("wait_for_gate"), call_order.index("stop_input_monitor"))
        self.assertLess(call_order.index("stop_input_monitor"), call_order.index("run_trial_sequence"))
        self.assertLess(call_order.index("run_trial_sequence"), call_order.index("Screen.__exit__"))


if __name__ == "__main__":
    unittest.main()
