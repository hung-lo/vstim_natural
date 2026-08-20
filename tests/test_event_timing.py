import csv
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
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


class EventTimingTests(unittest.TestCase):
    def test_display_raw_with_timing_captures_request_before_display(self):
        call_order = []
        timestamps = iter(
            [
                {
                    "unix_ns": 111,
                    "unix_sec": "111.000000000",
                    "utc_iso": "1970-01-01T00:00:00.111000000+00:00",
                },
                {
                    "unix_ns": 222,
                    "unix_sec": "222.000000000",
                    "utc_iso": "1970-01-01T00:00:00.222000000+00:00",
                },
            ]
        )
        perf_values = iter([1_000, 1_600])

        def fake_capture_timestamp():
            call_order.append("capture")
            return next(timestamps)

        def fake_perf_counter_ns():
            return next(perf_values)

        class FakeScreen:
            def display_raw(self, raw):
                call_order.append(("display_raw", raw))
                return SimpleNamespace(start_time=12.0, mean_interframe=34.0, stddev_interframe=5.0)

        with patch.object(base, "capture_timestamp", side_effect=fake_capture_timestamp), patch.object(
            base.time, "perf_counter_ns", side_effect=fake_perf_counter_ns
        ):
            perf, timing = base.display_raw_with_timing(FakeScreen(), "raw-frame")

        self.assertEqual(call_order, ["capture", ("display_raw", "raw-frame"), "capture"])
        self.assertEqual(perf.start_time, 12.0)
        self.assertEqual(timing["request_unix_ns"], 111)
        self.assertEqual(timing["return_unix_ns"], 222)
        self.assertEqual(timing["request_utc_iso"], "1970-01-01T00:00:00.111000000+00:00")
        self.assertEqual(timing["return_utc_iso"], "1970-01-01T00:00:00.222000000+00:00")
        self.assertAlmostEqual(timing["duration_sec"], 0.0000006)

    def test_append_csv_row_populates_consistent_timestamps(self):
        def fake_capture_timestamp():
            return {
                "unix_ns": 1_689_000_123_456_789_012,
                "unix_sec": "1689000123.456789012",
                "utc_iso": "2023-07-10T01:02:03.456789012+00:00",
            }

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(base, "capture_timestamp", side_effect=fake_capture_timestamp):
            path = Path(tmpdir) / "events.csv"
            base.append_csv_row(
                path,
                {"event_type": "session_start", "notes": "screen_opened"},
                ["utc_iso", "unix_time_utc_sec", "event_type", "notes"],
            )

            with path.open(newline="") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual(row["utc_iso"], "2023-07-10T01:02:03.456789012+00:00")
        self.assertEqual(row["unix_time_utc_sec"], "1689000123.456789012")
        self.assertEqual(row["event_type"], "session_start")
        self.assertEqual(row["notes"], "screen_opened")

    def test_prestim_gray_event_uses_display_request_timestamp(self):
        timing = {
            "request_utc_iso": "2026-01-01T00:00:00.000000000+00:00",
            "request_unix_sec": "1767225600.000000000",
            "request_unix_ns": 1767225600000000000,
            "return_utc_iso": "2026-01-01T00:00:01.000000000+00:00",
            "return_unix_ns": 1767225601000000000,
            "request_perf_counter_ns": 10,
            "return_perf_counter_ns": 1_000_000_010,
            "duration_sec": 1.0,
        }
        perf = SimpleNamespace(start_time=12.0, mean_interframe=34.0, stddev_interframe=5.0)

        row = cam.make_prestim_gray_event_row(Path("/tmp/gray.raw"), 60, perf, timing)

        self.assertEqual(row["utc_iso"], timing["request_utc_iso"])
        self.assertEqual(row["unix_time_utc_sec"], timing["request_unix_sec"])
        self.assertEqual(row["display_request_unix_ns"], timing["request_unix_ns"])
        self.assertEqual(row["display_return_utc_iso"], timing["return_utc_iso"])


    def test_run_trial_sequence_logs_stim_then_iti_without_interleaving(self):
        call_order = []
        rows = []

        def fake_display_raw_with_timing(screen, raw):
            call_order.append(("display", raw))
            if raw == "stim-raw":
                perf = SimpleNamespace(start_time=1.0, mean_interframe=2.0, stddev_interframe=3.0)
                timing = {
                    "request_unix_ns": 10,
                    "request_unix_sec": "10.000000000",
                    "request_utc_iso": "1970-01-01T00:00:10.000000000+00:00",
                    "return_unix_ns": 20,
                    "return_unix_sec": "20.000000000",
                    "return_utc_iso": "1970-01-01T00:00:20.000000000+00:00",
                    "request_perf_counter_ns": 100,
                    "return_perf_counter_ns": 200,
                    "duration_sec": 0.0000001,
                }
            else:
                perf = SimpleNamespace(start_time=4.0, mean_interframe=5.0, stddev_interframe=6.0)
                timing = {
                    "request_unix_ns": 30,
                    "request_unix_sec": "30.000000000",
                    "request_utc_iso": "1970-01-01T00:00:30.000000000+00:00",
                    "return_unix_ns": 40,
                    "return_unix_sec": "40.000000000",
                    "return_utc_iso": "1970-01-01T00:00:40.000000000+00:00",
                    "request_perf_counter_ns": 300,
                    "return_perf_counter_ns": 400,
                    "duration_sec": 0.0000001,
                }
            return perf, timing

        def fake_append_csv_row(path, row, fieldnames):
            rows.append((row["event_type"], dict(row)))
            call_order.append(("append", row["event_type"]))

        def fake_print_progress(trial_number, total_trials, start_time):
            call_order.append(("progress", trial_number, total_trials))

        trial = {
            "trial_index": 0,
            "repeat_number": 1,
            "image_index": 0,
            "image_id": 42,
            "image_filename": "img_42.png",
            "image_path": "/tmp/img_42.png",
        }

        with patch.object(base, "display_raw_with_timing", side_effect=fake_display_raw_with_timing), patch.object(
            base, "append_csv_row", side_effect=fake_append_csv_row
        ), patch.object(base, "print_progress", side_effect=fake_print_progress):
            base.run_trial_sequence(
                screen=object(),
                trials=[trial],
                loaded_stim_raws={"img_42": "stim-raw"},
                iti_raw="iti-raw",
                stim_raw_paths={"img_42": Path("/tmp/stim.raw")},
                iti_raw_path=Path("/tmp/iti.raw"),
                event_log_path=Path("/tmp/events.csv"),
                gpio=None,
            )

        self.assertEqual(
            call_order,
            [
                ("display", "stim-raw"),
                ("append", "stim_on"),
                ("display", "iti-raw"),
                ("append", "iti_on"),
                ("progress", 1, 1),
            ],
        )
        self.assertEqual(rows[0][1]["utc_iso"], "1970-01-01T00:00:10.000000000+00:00")
        self.assertEqual(rows[0][1]["display_request_unix_ns"], 10)
        self.assertEqual(rows[1][1]["utc_iso"], "1970-01-01T00:00:30.000000000+00:00")
        self.assertEqual(rows[1][1]["display_return_unix_ns"], 40)


if __name__ == "__main__":
    unittest.main()
