"""The canonical vision trainer's contracts, testable without a GPU.

Same architecture as test_training_deliverability.py: the mirrored directory is DERIVED
from deploy/03_storage.py's own upload function, never written down, so a trainer nothing
deploys cannot be green. The heavy imports in train_detection.py are lazy by design --
build_parser() and the data helpers must import on a machine with no torch.
"""
import importlib.util
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True   # keep __pycache__ out of the mirrored sourcedirs


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, REPO / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _vision_sourcedir() -> pathlib.Path:
    src = (REPO / "deploy/03_storage.py").read_text()
    body = src.split("def ensure_vision_code(")[1].split("\ndef ")[0]
    parts = re.findall(r'"([a-z_0-9]+)"', body.split("src_dir =")[1].split("\n")[0])
    assert parts, "ensure_vision_code no longer builds its source dir from literal parts"
    d = REPO.joinpath(*parts)
    assert d.is_dir(), f"ensure_vision_code mirrors {d}, which does not exist"
    return d


VISION_DIR = _vision_sourcedir()
trainer = _load("train_detection", str((VISION_DIR / "train_detection.py")
                                       .relative_to(REPO)))
ds_mod = _load("vision_dataset", str((VISION_DIR / "dataset.py").relative_to(REPO)))


class TestArgparseContract:
    def test_num_classes_is_required_not_inherited(self):
        with pytest.raises(SystemExit):
            trainer.build_parser().parse_args([])

    def test_the_deliverability_knobs_exist_with_reachable_defaults(self):
        args = trainer.build_parser().parse_args(["--num_classes", "3"])
        assert args.checkpoint_dir == "/opt/ml/checkpoints"
        assert args.max_train_seconds == 0
        assert args.model_family == "rtdetr"
        assert args.seed == 42

    def test_ultralytics_is_not_a_choice(self):
        with pytest.raises(SystemExit):
            trainer.build_parser().parse_args(
                ["--num_classes", "3", "--model_family", "yolo"])


class TestLicenseFirewall:
    #: The forms that actually create an AGPL surface: an import statement or a
    #: requirements line. Prose WARNING about the package is not a surface -- and a
    #: pattern that matched prose would match the very docstrings that state this rule
    #: (the self-scanning-guard failure: a scanner that reads its own vocabulary).
    _AGPL_SURFACE = re.compile(
        r"(?m)^\s*(?:import\s+ultralytics|from\s+ultralytics\b"
        r"|ultralytics(?:[=<>~\[]|\s*$))")

    def test_no_ultralytics_import_anywhere_in_vision_code(self):
        """The vendor asserts AGPL-3.0 over fine-tuned weights and the embedding
        application; one import or pip line here relicenses the customer's model.
        Scans the mirrored sourcedir AND the eval instrument directory."""
        offenders = []
        for d in (VISION_DIR, REPO / "pipeline" / "eval"):
            for p in d.rglob("*"):
                if p.is_file() and p.suffix in (".py", ".txt"):
                    if self._AGPL_SURFACE.search(p.read_text(errors="replace")):
                        offenders.append(str(p.relative_to(REPO)))
        assert not offenders, f"AGPL surface found in {offenders}"

    def test_the_firewall_pattern_is_not_decoration(self):
        """A guard that can never fire is a green light wearing a guard's name:
        the pattern must match the two real forms it exists to catch."""
        assert self._AGPL_SURFACE.search("import ultralytics\n")
        assert self._AGPL_SURFACE.search("from ultralytics import YOLO\n")
        assert self._AGPL_SURFACE.search("ultralytics==8.3.0\n")
        assert not self._AGPL_SURFACE.search(
            "# Ultralytics asserts AGPL-3.0 over trained weights\n")


class TestCocoHelpers:
    def test_coco_records_become_the_hf_annotation_shape(self):
        out = ds_mod.coco_to_hf_annotations(
            7, [{"bbox": [1, 2, 3, 4], "category_id": 2}])
        assert out["image_id"] == 7
        assert out["annotations"][0]["area"] == 12.0
        assert out["annotations"][0]["iscrowd"] == 0

    def test_newest_checkpoint_picks_the_highest_epoch(self, tmp_path):
        for e in (0, 2, 10):
            (tmp_path / f"epoch-{e}").mkdir()
        assert trainer.newest_checkpoint(str(tmp_path)).endswith("epoch-10")

    def test_no_checkpoint_dir_means_a_fresh_start(self, tmp_path):
        assert trainer.newest_checkpoint(str(tmp_path / "absent")) is None


class TestMirrorGuards:
    def _storage_with_root(self, monkeypatch, tmp_path):
        storage = _load("storage_mod", "deploy/03_storage.py")
        fake_file = tmp_path / "deploy" / "03_storage.py"
        fake_file.parent.mkdir(parents=True)
        monkeypatch.setattr(storage, "__file__", str(fake_file))
        return storage

    def test_an_empty_vision_sourcedir_refuses_to_upload(self, monkeypatch, tmp_path):
        """An empty mirror strands every vision launch behind a prompt that names it;
        the guard must fail the DEPLOY, not the run. Negative control m344 deletes it."""
        storage = self._storage_with_root(monkeypatch, tmp_path)
        (tmp_path / "pipeline" / "training" / "vision").mkdir(parents=True)
        with pytest.raises(SystemExit):
            storage.ensure_vision_code(None, "bucket", dry=True)

    def test_a_populated_dry_run_names_the_vision_prefix(self, monkeypatch, tmp_path):
        storage = self._storage_with_root(monkeypatch, tmp_path)
        d = tmp_path / "pipeline" / "training" / "vision"
        d.mkdir(parents=True)
        (d / "train_detection.py").write_text("x = 1\n")
        out = storage.ensure_vision_code(None, "bucket", dry=True)
        assert out["to"] == "s3://bucket/code/vision/"

    def test_the_scorer_rides_the_eval_instrument_mirror(self):
        """The mAP scorer lives in pipeline/eval/, so ensure_eval_instrument globs it
        with the judge prompt -- pinned here so a future move to its own directory
        does not silently leave it unmirrored and unreadable by the eval role."""
        assert (REPO / "pipeline" / "eval" / "vision_map_scorer.py").is_file()
        src = (REPO / "deploy/03_storage.py").read_text()
        body = src.split("def ensure_eval_instrument(")[1].split("\ndef ")[0]
        assert 'glob("*")' in body, (
            "ensure_eval_instrument no longer mirrors every file in pipeline/eval/ -- "
            "the vision scorer may have been left behind")
