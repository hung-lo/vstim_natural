import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

# Stub out PIL so the planning and cache helpers can be tested without Pillow.
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


class ItiJitterTests(unittest.TestCase):
    def image_files(self, count=8):
        return [Path("/tmp/image_%03d.png" % index) for index in range(count)]

    def make_trials(self, iti_seed=12345, trial_seed=67890, repeats=20):
        with patch.object(base, "ITI_JITTER_SEED", iti_seed), patch.object(base, "TRIAL_ORDER_SEED", trial_seed):
            return base.make_trial_sequence(self.image_files(), repeats)[0]

    def test_iti_range_and_frame_duration_consistency(self):
        trials = self.make_trials(repeats=20)
        minimum, maximum = base.iti_frame_bounds()

        self.assertTrue(all(minimum <= int(trial["planned_iti_frames"]) <= maximum for trial in trials))
        self.assertTrue(
            all(
                abs(
                    float(trial["planned_iti_duration_sec"])
                    - int(trial["planned_iti_frames"]) / base.REFRESH_RATE_HZ
                )
                < 1e-9
                for trial in trials
            )
        )

    def test_fixed_iti_seed_is_reproducible(self):
        first = self.make_trials(iti_seed=12345)
        second = self.make_trials(iti_seed=12345)

        self.assertEqual(
            [trial["planned_iti_frames"] for trial in first],
            [trial["planned_iti_frames"] for trial in second],
        )

    def test_iti_seed_is_independent_of_trial_order_seed(self):
        first = self.make_trials(iti_seed=12345, trial_seed=67890)
        second = self.make_trials(iti_seed=54321, trial_seed=67890)

        self.assertEqual(
            [trial["image_filename"] for trial in first],
            [trial["image_filename"] for trial in second],
        )
        self.assertNotEqual(
            [trial["planned_iti_frames"] for trial in first],
            [trial["planned_iti_frames"] for trial in second],
        )

    def test_exact_planned_duration_respects_final_iti_policy(self):
        trials = [
            {"planned_stim_duration_sec": 0.5, "planned_iti_duration_sec": 0.7},
            {"planned_stim_duration_sec": 0.5, "planned_iti_duration_sec": 0.8},
            {"planned_stim_duration_sec": 0.5, "planned_iti_duration_sec": 0.9},
        ]

        self.assertAlmostEqual(base.calculate_planned_sequence_duration(trials, True), 3.9)
        self.assertAlmostEqual(base.calculate_planned_sequence_duration(trials, False), 3.0)

    def test_camera_final_trial_is_marked_without_an_iti(self):
        trials = self.make_trials(repeats=1)
        base.set_iti_playback_flags(trials, include_final_iti=False)

        self.assertEqual(sum(bool(trial["iti_will_play"]) for trial in trials), len(trials) - 1)
        self.assertFalse(trials[-1]["iti_will_play"])
        self.assertAlmostEqual(
            base.calculate_planned_sequence_duration(trials, include_final_iti=False),
            sum(float(trial["planned_stim_duration_sec"]) for trial in trials)
            + sum(float(trial["planned_iti_duration_sec"]) for trial in trials[:-1]),
        )

    def test_iti_raw_cache_only_builds_the_19_permitted_frame_counts(self):
        calls = []

        with TemporaryDirectory() as temporary_directory:
            def fake_convert(rpg_module, canvas, raw_path, duration_sec):
                calls.append((Path(raw_path), duration_sec))
                return raw_path

            with patch.object(base, "build_canvas", return_value=object()), patch.object(
                base, "convert_canvas_to_rpg_raw", side_effect=fake_convert
            ):
                paths = base.build_iti_raw_cache(object(), Path(temporary_directory))

        self.assertEqual(set(paths), set(range(42, 61)))
        self.assertEqual(len(calls), 19)
        self.assertEqual(
            {round(duration * base.REFRESH_RATE_HZ) for _, duration in calls},
            set(range(42, 61)),
        )


if __name__ == "__main__":
    unittest.main()
