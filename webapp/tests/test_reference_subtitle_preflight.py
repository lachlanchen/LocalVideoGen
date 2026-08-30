from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[2] / "scripts" / "preflight_reference_subtitles.py"
SPEC = importlib.util.spec_from_file_location("preflight_reference_subtitles", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _tsv(*rows: tuple[str, float, int, int, int, int, int]) -> str:
    header = (
        "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\t"
        "left\ttop\twidth\theight\tconf\ttext"
    )
    body = []
    for word_num, (text, confidence, left, top, width, height, line_num) in enumerate(rows, 1):
        body.append(
            f"5\t1\t1\t1\t{line_num}\t{word_num}\t{left}\t{top}\t{width}\t{height}\t{confidence}\t{text}"
        )
    return "\n".join([header, *body])


class CandidateParsingTests(unittest.TestCase):
    def test_groups_chinese_caption_and_restores_full_frame_y(self):
        candidates = MODULE.candidates_from_tsv(
            _tsv(
                ("今天", 91.0, 80, 20, 42, 24, 1),
                ("去马德拉", 88.0, 130, 20, 96, 24, 1),
            ),
            frame_width=576,
            crop_y=560,
            minimum_confidence=55,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].text, "今天去马德拉")
        self.assertEqual(candidates[0].y, 580)
        self.assertEqual(candidates[0].x, 80)

    def test_rejects_low_confidence_and_one_character_noise(self):
        candidates = MODULE.candidates_from_tsv(
            _tsv(
                ("字幕", 22.0, 40, 10, 55, 20, 1),
                ("x", 96.0, 160, 15, 20, 22, 2),
            ),
            frame_width=1024,
            crop_y=420,
            minimum_confidence=55,
        )
        self.assertEqual(candidates, [])

    def test_accepts_four_character_latin_caption(self):
        candidates = MODULE.candidates_from_tsv(
            _tsv(("hello", 86.0, 100, 30, 76, 24, 1)),
            frame_width=1024,
            crop_y=420,
            minimum_confidence=55,
        )
        self.assertEqual([item.text for item in candidates], ["hello"])

    def test_restores_coordinates_after_three_x_preprocessing(self):
        candidates = MODULE.candidates_from_tsv(
            _tsv(
                ("也是", 91.0, 300, 45, 150, 72, 1),
                ("马德拉", 89.0, 480, 45, 240, 72, 1),
            ),
            frame_width=576,
            crop_y=700,
            minimum_confidence=55,
            coordinate_scale=3,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].x, 100)
        self.assertEqual(candidates[0].y, 715)
        self.assertEqual(candidates[0].width, 140)
        self.assertEqual(candidates[0].height, 24)

    def test_adjacent_real_text_is_persistent_but_stroke_noise_is_not(self):
        real_before = MODULE.TextCandidate("也是球C", 76, 149, 745, 174, 41)
        real_after = MODULE.TextCandidate("是是C的", 90, 182, 751, 208, 53)
        self.assertEqual(
            MODULE.persistent_candidate_pair(
                [real_before], [real_after], frame_width=576
            ),
            (real_before, real_after),
        )

        noisy_before = MODULE.TextCandidate("一二", 58, 486, 428, 259, 224)
        noisy_after = MODULE.TextCandidate("二一", 57, 506, 631, 119, 14)
        self.assertIsNone(
            MODULE.persistent_candidate_pair(
                [noisy_before], [noisy_after], frame_width=1024
            )
        )

    def test_identical_common_character_caption_is_persistent(self):
        before = MODULE.TextCandidate("一个人", 92, 320, 650, 160, 36)
        after = MODULE.TextCandidate("一个人", 94, 324, 651, 158, 35)
        self.assertEqual(
            MODULE.persistent_candidate_pair([before], [after], frame_width=1024),
            (before, after),
        )

        displaced_noise = MODULE.TextCandidate("一个人", 94, 520, 610, 90, 22)
        self.assertIsNone(
            MODULE.persistent_candidate_pair(
                [before], [displaced_noise], frame_width=1024
            )
        )

    def test_common_character_caption_survives_ocr_reordering(self):
        before = MODULE.TextCandidate("一个人", 88, 320, 650, 160, 36)
        after = MODULE.TextCandidate("一人个", 86, 323, 651, 158, 35)
        self.assertEqual(
            MODULE.persistent_candidate_pair([before], [after], frame_width=1024),
            (before, after),
        )

    def test_candidate_dedupe_keeps_highest_confidence(self):
        lower = MODULE.TextCandidate("马德拉", 70, 300, 650, 180, 40)
        higher = MODULE.TextCandidate("马德拉", 91, 300, 650, 180, 40)
        self.assertEqual(MODULE._dedupe_candidates([lower, higher]), [higher])


class CliGuardTests(unittest.TestCase):
    def test_sample_plan_requires_two_observations(self):
        with self.assertRaisesRegex(MODULE.PreflightError, "fewer than two"):
            MODULE._expected_sample_count(2.0, 0.25)
        self.assertEqual(MODULE._expected_sample_count(2.0, 1.0), 2)

    def test_missing_operator_declaration_fails_closed_before_io(self):
        self.assertEqual(MODULE.main(["does-not-need-to-exist.mp4"]), 2)

    def test_invalid_scan_range_fails_closed(self):
        self.assertEqual(
            MODULE.main(
                [
                    "does-not-need-to-exist.mp4",
                    "--declare-subtitle-free",
                    "--sample-fps",
                    "0",
                ]
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
