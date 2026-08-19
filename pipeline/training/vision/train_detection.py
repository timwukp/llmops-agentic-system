"""Canonical detection fine-tune for SageMaker script mode -- RT-DETR first, D-FINE vendored.

This sourcedir is CANONICAL, not authored: the finetune agent downloads it from
s3://<bucket>/code/vision/ (mirrored by deploy/03_storage.py with byte read-back) and
packages it verbatim as sourcedir.tar.gz. A self-written trainer is the FALLBACK for when
the canonical one is unreachable, and must be declared in evidence -- same contract as
code/distill/train_qlora.py.

Licensing is a design input, not a footnote: this file supports ONLY Apache-2.0 model
families (RT-DETR via HuggingFace Transformers; D-FINE via a pinned-sha vendor directory).
Ultralytics packages must never appear here or in requirements.txt -- the vendor asserts
AGPL-3.0 over trained weights and the embedding application, and one transitive import is
the whole platform's licensing story gone. tests/test_vision_trainer.py greps for it.

Deliverability rules (inherited from the QLoRA trainer, same failure economics):
  - checkpoints go to --checkpoint_dir (/opt/ml/checkpoints -> S3 sync) every epoch, so a
    MaxRuntime kill never costs more than one epoch;
  - --max_train_seconds triggers a GRACEFUL stop that still saves and exits 0: a save
    point that lies beyond MaxRuntime is not a save point (validate_job_config.py);
  - resumes from the newest checkpoint in --checkpoint_dir when one exists.

Data contract (COCO-format, prepared by the data-prep agent):
  --train_uri / --val_uri each name a directory containing images/ and annotations.json
  (COCO: images[], annotations[] with bbox [x,y,w,h] absolute, categories[]).
"""
import argparse
import json
import os
import sys
import time


def build_parser():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--model_family", choices=("rtdetr", "dfine"), default="rtdetr",
                    help="Apache-2.0 detector family; ultralytics is refused by design")
    ap.add_argument("--model_id", default="PekingU/rtdetr_r50vd",
                    help="HF hub id (rtdetr) or vendored config name (dfine)")
    ap.add_argument("--model_revision", default=None,
                    help="pinned revision; unpinned weights are an unaudited input")
    ap.add_argument("--train_dir", default=os.environ.get("SM_CHANNEL_TRAIN", "/opt/ml/input/data/train"))
    ap.add_argument("--val_dir", default=os.environ.get("SM_CHANNEL_VAL", "/opt/ml/input/data/val"))
    ap.add_argument("--output_dir", default=os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))
    ap.add_argument("--checkpoint_dir", default="/opt/ml/checkpoints")
    ap.add_argument("--epochs", type=int, default=20)
    ap.add_argument("--batch_size", type=int, default=4)
    ap.add_argument("--learning_rate", type=float, default=1e-4)
    ap.add_argument("--weight_decay", type=float, default=1e-4)
    ap.add_argument("--max_train_seconds", type=int, default=0,
                    help="0 = no budget; otherwise stop gracefully, save, exit 0")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--num_classes", type=int, required=True,
                    help="category count; silently inheriting it from a checkpoint trained "
                         "on different classes is a shape error a GPU-hour later")
    return ap


def load_coco_dir(path):
    """(images list, annotations-by-image, categories) from a COCO directory."""
    with open(os.path.join(path, "annotations.json")) as fh:
        coco = json.load(fh)
    by_image = {}
    for a in coco.get("annotations", []):
        by_image.setdefault(a["image_id"], []).append(a)
    return coco.get("images", []), by_image, coco.get("categories", [])


def newest_checkpoint(checkpoint_dir):
    if not os.path.isdir(checkpoint_dir):
        return None
    cands = [d for d in os.listdir(checkpoint_dir) if d.startswith("epoch-")]
    if not cands:
        return None
    return os.path.join(checkpoint_dir,
                        max(cands, key=lambda d: int(d.split("-", 1)[1])))


def train(args):
    # Heavy imports live here so the argparse/data contracts stay testable on machines
    # (and CI runners) that install neither torch nor transformers.
    import torch  # noqa: F401
    from transformers import AutoImageProcessor

    if args.model_family == "rtdetr":
        from transformers import RTDetrForObjectDetection as DetModel
        model = DetModel.from_pretrained(
            args.model_id, revision=args.model_revision,
            num_labels=args.num_classes, ignore_mismatched_sizes=True)
        processor = AutoImageProcessor.from_pretrained(
            args.model_id, revision=args.model_revision)
    else:
        # D-FINE is Apache-2.0 but not in transformers: it ships as a vendor directory
        # (pinned sha, mirrored beside this file). Refusing loudly beats importing a
        # lookalike from PyPI that nobody audited.
        vendor = os.path.join(os.path.dirname(__file__), "d_fine_vendor")
        if not os.path.isdir(vendor):
            raise SystemExit(
                "model_family=dfine requires the pinned d_fine_vendor/ directory in this "
                "sourcedir; it is mirrored separately. Use --model_family rtdetr, or "
                "mirror the vendor tree first.")
        sys.path.insert(0, vendor)
        from dfine_adapter import load_dfine  # noqa: provided by the vendor tree
        model, processor = load_dfine(args.model_id, args.num_classes)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model.to(device)
    torch.manual_seed(args.seed)

    images, anns, cats = load_coco_dir(args.train_dir)
    started = time.time()
    start_epoch = 0
    resume = newest_checkpoint(args.checkpoint_dir)
    if resume:
        state = torch.load(os.path.join(resume, "training_state.pt"),
                           map_location=device)
        model.load_state_dict(state["model"])
        start_epoch = int(state["epoch"]) + 1
        print(f"[trainer] resumed from {resume} at epoch {start_epoch}", flush=True)

    optim = torch.optim.AdamW(model.parameters(), lr=args.learning_rate,
                              weight_decay=args.weight_decay)
    from torch.utils.data import DataLoader
    from dataset import CocoDetectionDataset, collate  # sibling file in this sourcedir
    ds = CocoDetectionDataset(args.train_dir, images, anns, processor)
    loader = DataLoader(ds, batch_size=args.batch_size, shuffle=True,
                        collate_fn=collate, num_workers=2)

    stopped_early = False
    for epoch in range(start_epoch, args.epochs):
        model.train()
        for step, batch in enumerate(loader):
            batch = {k: (v.to(device) if hasattr(v, "to") else v)
                     for k, v in batch.items()}
            out = model(**batch)
            out.loss.backward()
            optim.step()
            optim.zero_grad()
            if step % 50 == 0:
                print(f"[trainer] epoch {epoch} step {step} loss {out.loss.item():.4f}",
                      flush=True)
            if args.max_train_seconds and time.time() - started > args.max_train_seconds:
                print("[trainer] time budget reached: graceful stop", flush=True)
                stopped_early = True
                break
        ckpt = os.path.join(args.checkpoint_dir, f"epoch-{epoch}")
        os.makedirs(ckpt, exist_ok=True)
        torch.save({"model": model.state_dict(), "epoch": epoch},
                   os.path.join(ckpt, "training_state.pt"))
        print(f"[trainer] checkpoint saved: {ckpt}", flush=True)
        if stopped_early:
            break

    os.makedirs(args.output_dir, exist_ok=True)
    model.save_pretrained(args.output_dir)
    processor.save_pretrained(args.output_dir)
    with open(os.path.join(args.output_dir, "training_summary.json"), "w") as fh:
        json.dump({"epochs_completed": epoch + 1 if not stopped_early else epoch,
                   "stopped_early": stopped_early,
                   "model_family": args.model_family, "model_id": args.model_id,
                   "num_classes": args.num_classes, "seed": args.seed}, fh)
    print("[trainer] done", flush=True)
    return 0


def main(argv=None):
    return train(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
