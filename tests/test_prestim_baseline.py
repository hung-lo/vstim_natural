import sys
import threading
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

import run_stringer_vstim as base
import run_stringer_vstim_cam as cam


class FakeClock:
    def __init__(self, now):
        self.now = now

    def monotonic(self):
        return self.now


class FakeGateEvent:
    def __init__(self, clock, scripted_returns=None):
        self.clock = clock
        self.scripted_returns = list(scripted_returns or [])
        self.wait_timeouts = []
        self._set = False

    def is_set(self):
        return self._set

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        if self.scripted_returns:
            result = self.scripted_returns.pop(0)
        else:
            result = False
        if not result:
            self.clock.now += timeout
        else:
            self._set = True
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

    def test_wait_for_prestimulus_gate_timer_satisfied_during_preparation(self):
        clock = FakeClock(200.0)
        event = FakeGateEvent(clock)
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(0.0, 180.0, event)

        self.assertEqual(result["end_reason"], "timer_satisfied_during_preparation")
        self.assertFalse(result["forced"])
        self.assertEqual(result["remaining_sec_at_gate_entry"], 0.0)
        self.assertAlmostEqual(result["elapsed_sec"], 200.0)
        self.assertEqual(event.wait_timeouts, [])

    def test_wait_for_prestimulus_gate_normal_wait_and_override(self):
        clock = FakeClock(120.0)
        event = FakeGateEvent(clock)
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(0.0, 180.0, event)

        self.assertEqual(result["end_reason"], "timer_elapsed")
        self.assertFalse(result["forced"])
        self.assertEqual(result["remaining_sec_at_gate_entry"], 60.0)
        self.assertAlmostEqual(result["elapsed_sec"], 180.0)
        self.assertEqual(sum(event.wait_timeouts), 60.0)
        self.assertTrue(all(timeout <= cam.BASELINE_STATUS_INTERVAL_SEC for timeout in event.wait_timeouts))
        self.assertEqual(len(event.wait_timeouts), 6)

        clock = FakeClock(120.0)
        event = FakeGateEvent(clock, scripted_returns=[True])
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(0.0, 180.0, event)

        self.assertEqual(result["end_reason"], "user_override")
        self.assertTrue(result["forced"])
        self.assertEqual(result["remaining_sec_at_gate_entry"], 60.0)
        self.assertAlmostEqual(result["elapsed_sec"], 120.0)
        self.assertEqual(event.wait_timeouts, [10.0])

    def test_wait_for_prestimulus_gate_zero_baseline_and_early_input_thread(self):
        clock = FakeClock(100.0)
        event = FakeGateEvent(clock)
        with patch.object(cam.time, "monotonic", side_effect=clock.monotonic), patch("builtins.print"):
            result = cam.wait_for_prestimulus_gate(100.0, 0.0, event)

        self.assertEqual(result["end_reason"], "timer_satisfied_during_preparation")
        self.assertFalse(result["forced"])
        self.assertEqual(result["remaining_sec_at_gate_entry"], 0.0)
        self.assertEqual(event.wait_timeouts, [])

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

    def test_watch_for_early_start_sets_event_on_yes(self):
        force_event = threading.Event()
        stop_event = threading.Event()
        fake_stdin = FakeStdin(["y\n"], tty=True)

        with patch.object(cam.sys, "stdin", fake_stdin), patch.object(cam.select, "select", side_effect=[([fake_stdin], [], [])]), patch("builtins.print"):
            thread = threading.Thread(target=cam.watch_for_early_start, args=(force_event, stop_event), daemon=True)
            thread.start()
            thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertTrue(force_event.is_set())


if __name__ == "__main__":
    unittest.main()
