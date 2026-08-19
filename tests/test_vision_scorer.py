"""The vision gate's instrument, pinned by hand-computed fixtures.

Every expected number in here was derived on paper from the COCO definition (greedy
score-ordered matching, 101-point interpolated AP), not by running the scorer and
pasting its output back -- an instrument tested against itself measures nothing.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True   # an import must not leave __pycache__ in
# pipeline/eval/: the instrument-set guard globs that directory, and a stray
# cache dir there is the .pyc-serves-the-mutant failure wearing a new hat
sys.path.insert(0, str(REPO / "pipeline" / "eval"))

import vision_map_scorer as vms  # noqa: E402


def _gt(*boxes, cat=1):
    return [{"bbox": list(b), "category_id": cat} for b in boxes]


def _pred(*items, cat=1):
    return [{"bbox": list(b), "category_id": cat, "score": s} for b, s in items]


class TestHandComputedAP:
    def test_perfect_predictions_score_ap50_one(self):
        gt = {"a": _gt([0, 0, 10, 10], [50, 50, 20, 20])}
        pr = {"a": _pred(([0, 0, 10, 10], 0.9), ([50, 50, 20, 20], 0.8))}
        out = vms.score(gt, pr)
        assert out["map50"] == 1.0
        assert out["map50_95"] == 1.0

    def test_one_true_positive_of_two_ground_truths_is_51_of_101_points(self):
        # TP at recall 0.5 with precision 1.0; the 101-point mean is 51/101.
        gt = {"a": _gt([0, 0, 10, 10], [50, 50, 20, 20])}
        pr = {"a": _pred(([0, 0, 10, 10], 0.9))}
        assert vms.score(gt, pr)["map50"] == round(51 / 101, 4)

    def test_a_confident_false_positive_ranked_above_the_hit_halves_precision(self):
        # FP at score .9 then TP at .8: precision at recall 0.5 is 0.5, AP = 51*0.5/101.
        gt = {"a": _gt([0, 0, 10, 10], [50, 50, 20, 20])}
        pr = {"a": _pred(([200, 200, 5, 5], 0.9), ([0, 0, 10, 10], 0.8))}
        assert vms.score(gt, pr)["map50"] == round(51 * 0.5 / 101, 4)

    def test_iou_exactly_at_the_threshold_matches(self):
        # GT [0,0,10,10] vs pred [0,0,10,5]: inter 50, union 100 -> IoU exactly 0.50.
        # COCO's convention is >=, so this MUST count. The mutation that flips the
        # comparator to > is negative control m343.
        gt = {"a": _gt([0, 0, 10, 10])}
        pr = {"a": _pred(([0, 0, 10, 5], 0.9))}
        assert vms.score(gt, pr)["map50"] == 1.0

    def test_an_iou_just_below_the_threshold_does_not_match(self):
        # pred [0,0,10,4.99]: inter 49.9, union 100.0 -> IoU 0.499 < 0.50.
        gt = {"a": _gt([0, 0, 10, 10])}
        pr = {"a": _pred(([0, 0, 10, 4.99], 0.9))}
        assert vms.score(gt, pr)["map50"] == 0.0

    def test_empty_ground_truth_image_makes_every_prediction_there_a_false_positive(self):
        gt = {"a": _gt([0, 0, 10, 10]), "b": []}
        pr = {"a": _pred(([0, 0, 10, 10], 0.8)),
              "b": _pred(([0, 0, 10, 10], 0.9))}   # higher-ranked FP on the empty image
        assert vms.score(gt, pr)["map50"] == round(101 * 0.5 / 101, 4)

    def test_a_category_absent_from_ground_truth_is_excluded_not_zeroed(self):
        # Predictions for a category no GT names must not drag the mean to zero --
        # undefined is not the same number as wrong.
        gt = {"a": _gt([0, 0, 10, 10], cat=1)}
        pr = {"a": _pred(([0, 0, 10, 10], 0.9), cat=1)
              + _pred(([50, 50, 5, 5], 0.9), cat=7)}
        assert vms.score(gt, pr)["map50"] == 1.0


class TestFormatValidity:
    def test_degenerate_boxes_are_invalid_and_excluded_from_matching(self):
        raw = {"a": [{"bbox": [0, 0, 0, 10], "category_id": 1, "score": 0.9},
                     {"bbox": [0, 0, 10, 10], "category_id": 1, "score": 0.8}]}
        clean, invalid = vms.validate_predictions(raw)
        assert invalid == 1 and len(clean["a"]) == 1

    def test_a_boolean_score_is_not_a_score(self):
        raw = {"a": [{"bbox": [0, 0, 10, 10], "category_id": 1, "score": True}]}
        clean, invalid = vms.validate_predictions(raw)
        assert invalid == 1 and clean["a"] == []

    def test_malformed_records_never_crash_the_scorer(self):
        raw = {"a": [None, {}, {"bbox": "nope"}, 7]}
        clean, invalid = vms.validate_predictions(raw)
        assert invalid == 4 and clean["a"] == []


class TestDeterminism:
    def test_the_bootstrap_is_seeded_and_reruns_agree(self):
        gt = {str(i): _gt([0, 0, 10, 10]) for i in range(8)}
        pr = {str(i): _pred(([0, 0, 10, 10], 0.9)) if i % 2 else []
              for i in range(8)}
        a = vms.bootstrap_ci(gt, pr, n_boot=50, seed=7)
        b = vms.bootstrap_ci(gt, pr, n_boot=50, seed=7)
        assert a == b

    def test_the_cli_emits_identical_json_on_identical_inputs(self, tmp_path):
        gt = {"a": _gt([0, 0, 10, 10])}
        pr = {"a": _pred(([0, 0, 10, 10], 0.9))}
        g, p = tmp_path / "gt.json", tmp_path / "pr.json"
        g.write_text(json.dumps(gt)), p.write_text(json.dumps(pr))
        runs = [subprocess.run(
            [sys.executable, str(REPO / "pipeline/eval/vision_map_scorer.py"),
             "--ground-truth", str(g), "--predictions", str(p),
             "--bootstrap", "20", "--seed", "3"],
            capture_output=True, text=True) for _ in range(2)]
        assert runs[0].returncode == 0
        assert runs[0].stdout == runs[1].stdout
        doc = json.loads(runs[0].stdout)
        assert doc["map50"] == 1.0 and doc["format_validity"] == 1.0
