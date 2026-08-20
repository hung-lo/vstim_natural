import random
import sys
import types
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

# Stub out PIL so the stimulus module can be imported without the Pi image stack.
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


def image_files(count):
    return [Path("/tmp/image_%03d.png" % index) for index in range(count)]


def sequence_signature(trials):
    return [(trial["image_id"], trial["repeat_number"]) for trial in trials]


class TrialSequenceTests(unittest.TestCase):
    def test_full_protocol_preserves_counts_and_avoids_adjacent_repeats(self):
        with patch.object(base, "TRIAL_ORDER_SEED", 12345), patch.object(base, "ITI_JITTER_SEED", 67890), patch.object(
            base, "AVOID_ADJACENT_REPEATS", True
        ):
            trials, _, _ = base.make_trial_sequence(image_files(32), 60)

        self.assertEqual(len(trials), 1920)
        self.assertEqual(Counter(trial["image_id"] for trial in trials), Counter({index: 60 for index in range(32)}))
        self.assertEqual(base.count_adjacent_image_repeats(trials), 0)

    def test_fixed_seed_is_reproducible(self):
        with patch.object(base, "TRIAL_ORDER_SEED", 2468), patch.object(base, "ITI_JITTER_SEED", 1357), patch.object(
            base, "AVOID_ADJACENT_REPEATS", True
        ):
            first, first_seed, first_iti_seed = base.make_trial_sequence(image_files(8), 5)
            second, second_seed, second_iti_seed = base.make_trial_sequence(image_files(8), 5)

        self.assertEqual((first_seed, first_iti_seed), (second_seed, second_iti_seed))
        self.assertEqual(sequence_signature(first), sequence_signature(second))
        self.assertEqual(first, second)

    def test_different_seed_produces_a_different_valid_order(self):
        with patch.object(base, "ITI_JITTER_SEED", 1357), patch.object(base, "AVOID_ADJACENT_REPEATS", True), patch.object(
            base, "TRIAL_ORDER_SEED", 1
        ):
            first, _, _ = base.make_trial_sequence(image_files(8), 5)
        with patch.object(base, "ITI_JITTER_SEED", 1357), patch.object(base, "AVOID_ADJACENT_REPEATS", True), patch.object(
            base, "TRIAL_ORDER_SEED", 2
        ):
            second, _, _ = base.make_trial_sequence(image_files(8), 5)

        self.assertNotEqual(sequence_signature(first), sequence_signature(second))
        self.assertEqual(base.count_adjacent_image_repeats(first), 0)
        self.assertEqual(base.count_adjacent_image_repeats(second), 0)

    def test_impossible_counts_raise_before_playback(self):
        trials = [{"image_id": "dominant", "repeat_number": index} for index in range(4)]
        trials.append({"image_id": "other", "repeat_number": 1})

        with self.assertRaisesRegex(ValueError, "Cannot avoid adjacent repeated images"):
            base.shuffle_trials_without_adjacent_images(trials, random.Random(99))


if __name__ == "__main__":
    unittest.main()
