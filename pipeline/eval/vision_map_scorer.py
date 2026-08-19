"""Deterministic COCO-style mAP scorer -- this file IS the vision gate's instrument.

Like `judge_prompt_pairwise.md` one directory over, this file is mirrored verbatim to
s3://<bucket>/code/vision/ by deploy/03_storage.py and the eval agent is instructed to
run it UNMODIFIED and record its sha256 in the manifest. A gate scored by an instrument
the scored party authored is a self-report; a gate scored by this file is a measurement.

Scope, stated plainly:
  - COCO-format boxes only: [x, y, w, h] in absolute pixels, category ids are opaque ints.
  - AP@0.50 (the gate metric) and AP@[.50:.95] (reported), 101-point interpolation,
    greedy score-ordered matching -- the pycocotools algorithm, reimplemented in
    numpy so the eval container needs no compiled dependency.
  - format_validity: the fraction of prediction records that are well-formed. A record
    with a degenerate box (w<=0 or h<=0), a non-numeric score, or a missing field is
    counted invalid and EXCLUDED from matching -- an invalid prediction must cost the
    model twice (validity and recall), never crash the scorer.
  - Bootstrap CI over IMAGES (the sampling unit a new image actually adds), seeded, so
    two runs of this file over the same inputs emit byte-identical JSON.

Usage (inside the eval job or locally):
    python vision_map_scorer.py --ground-truth gt.json --predictions pred.json \
        [--bootstrap 1000] [--seed 42] [--json out.json]

gt.json:   {"<image_id>": [{"bbox": [x,y,w,h], "category_id": int}, ...], ...}
pred.json: {"<image_id>": [{"bbox": [x,y,w,h], "category_id": int, "score": float}, ...]}
Exit code: 0 always when scoring completes; the GATE decision belongs to the eval agent
reading the numbers, not to the instrument that produced them.
"""
import argparse
import json
import sys

import numpy as np

IOU_THRESHOLDS = np.round(np.arange(0.50, 1.00, 0.05), 2)   # .50, .55, ... .95
RECALL_POINTS = np.linspace(0.0, 1.0, 101)                  # COCO 101-point interp


def _valid_box(b):
    return (isinstance(b, (list, tuple)) and len(b) == 4
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in b)
            and float(b[2]) > 0 and float(b[3]) > 0)


def validate_predictions(preds_by_image):
    """Split predictions into (well-formed, invalid_count). Never raises on shape."""
    clean, invalid = {}, 0
    for img, records in (preds_by_image or {}).items():
        kept = []
        for r in records if isinstance(records, list) else []:
            ok = (isinstance(r, dict) and _valid_box(r.get("bbox"))
                  and isinstance(r.get("category_id"), int)
                  and isinstance(r.get("score"), (int, float))
                  and not isinstance(r.get("score"), bool)
                  and 0.0 <= float(r["score"]) <= 1.0)
            if ok:
                kept.append(r)
            else:
                invalid += 1
        clean[str(img)] = kept
    return clean, invalid


def _iou_matrix(det, gt):
    """IoU of every detection box against every ground-truth box. [x,y,w,h] absolute."""
    if not len(det) or not len(gt):
        return np.zeros((len(det), len(gt)))
    d, g = np.asarray(det, dtype=float), np.asarray(gt, dtype=float)
    dx1, dy1, dx2, dy2 = d[:, 0], d[:, 1], d[:, 0] + d[:, 2], d[:, 1] + d[:, 3]
    gx1, gy1, gx2, gy2 = g[:, 0], g[:, 1], g[:, 0] + g[:, 2], g[:, 1] + g[:, 3]
    ix1 = np.maximum(dx1[:, None], gx1[None, :])
    iy1 = np.maximum(dy1[:, None], gy1[None, :])
    ix2 = np.minimum(dx2[:, None], gx2[None, :])
    iy2 = np.minimum(dy2[:, None], gy2[None, :])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    area_d = (d[:, 2] * d[:, 3])[:, None]
    area_g = (g[:, 2] * g[:, 3])[None, :]
    union = area_d + area_g - inter
    return np.where(union > 0, inter / union, 0.0)


def _match_category(dets, gts, iou_thr):
    """Greedy COCO matching for one (image, category): score-desc, best available GT.

    A detection matches the highest-IoU ground truth not yet claimed, if that IoU is
    >= iou_thr (COCO's convention: the threshold itself passes). Returns
    (tp_flags aligned to score-sorted dets, n_gt).
    """
    order = sorted(range(len(dets)), key=lambda i: (-float(dets[i]["score"]), i))
    ious = _iou_matrix([dets[i]["bbox"] for i in order], [g["bbox"] for g in gts])
    claimed = np.zeros(len(gts), dtype=bool)
    tp = np.zeros(len(order), dtype=bool)
    for row, _ in enumerate(order):
        best, best_iou = -1, iou_thr
        for col in range(len(gts)):
            if not claimed[col] and ious[row, col] >= best_iou:
                best, best_iou = col, ious[row, col]
        if best >= 0:
            claimed[best] = True
            tp[row] = True
    scores = np.array([float(dets[i]["score"]) for i in order])
    return tp, scores


def _average_precision(tp, scores, n_gt):
    """101-point interpolated AP from per-detection TP flags (global score order)."""
    if n_gt == 0:
        return None            # category absent from GT: undefined, excluded from mean
    if not len(tp):
        return 0.0
    order = np.argsort(-scores, kind="stable")
    tp = tp[order]
    cum_tp = np.cumsum(tp)
    cum_fp = np.cumsum(~tp)
    recall = cum_tp / n_gt
    precision = cum_tp / np.maximum(cum_tp + cum_fp, 1)
    # precision envelope (monotone non-increasing from the right)
    for i in range(len(precision) - 2, -1, -1):
        precision[i] = max(precision[i], precision[i + 1])
    idx = np.searchsorted(recall, RECALL_POINTS, side="left")
    p = np.where(idx < len(precision), precision[np.minimum(idx, len(precision) - 1)], 0.0)
    return float(np.mean(p))


def score(gt_by_image, preds_by_image, image_ids=None):
    """AP50 / AP[.50:.95] over the given images (default: every image in GT)."""
    images = sorted(str(i) for i in (image_ids if image_ids is not None else gt_by_image))
    cats = sorted({g["category_id"] for img in images for g in gt_by_image.get(img, [])})
    ap_per_thr = []
    ap50_per_cat = {}
    for thr in IOU_THRESHOLDS:
        aps = []
        for cat in cats:
            tps, scoreses, n_gt = [], [], 0
            for img in images:
                gts = [g for g in gt_by_image.get(img, []) if g["category_id"] == cat]
                dets = [d for d in preds_by_image.get(img, []) if d["category_id"] == cat]
                n_gt += len(gts)
                if dets:
                    tp, sc = _match_category(dets, gts, thr)
                    tps.append(tp), scoreses.append(sc)
            tp = np.concatenate(tps) if tps else np.zeros(0, dtype=bool)
            sc = np.concatenate(scoreses) if scoreses else np.zeros(0)
            ap = _average_precision(tp, sc, n_gt)
            if ap is not None:
                aps.append(ap)
                if abs(thr - 0.50) < 1e-9:
                    ap50_per_cat[str(cat)] = round(ap, 4)
        ap_per_thr.append(float(np.mean(aps)) if aps else 0.0)
    return {"map50": round(ap_per_thr[0], 4),
            "map50_95": round(float(np.mean(ap_per_thr)), 4),
            "ap50_per_category": ap50_per_cat,
            "images": len(images), "categories": len(cats)}


def bootstrap_ci(gt_by_image, preds_by_image, n_boot=1000, seed=42, metric="map50"):
    """Percentile 95% CI by resampling IMAGES with replacement. Seeded: reruns agree."""
    images = sorted(gt_by_image)
    if not images:
        return [0.0, 0.0]
    rng = np.random.RandomState(seed)
    vals = []
    for _ in range(n_boot):
        sample = [images[i] for i in rng.randint(0, len(images), len(images))]
        # resampled images may repeat; score() dedups via image_ids set semantics,
        # so build an explicit multiset view by suffixing duplicates
        gt_s, pr_s = {}, {}
        for j, img in enumerate(sample):
            key = f"{img}#{j}"
            gt_s[key] = gt_by_image.get(img, [])
            pr_s[key] = preds_by_image.get(img, [])
        vals.append(score(gt_s, pr_s)[metric])
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return [round(float(lo), 4), round(float(hi), 4)]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ground-truth", required=True)
    ap.add_argument("--predictions", required=True)
    ap.add_argument("--bootstrap", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json", help="write the result document here as well as stdout")
    args = ap.parse_args(argv)
    with open(args.ground_truth) as fh:
        gt = {str(k): v for k, v in json.load(fh).items()}
    with open(args.predictions) as fh:
        raw = {str(k): v for k, v in json.load(fh).items()}
    preds, invalid = validate_predictions(raw)
    total = invalid + sum(len(v) for v in preds.values())
    out = score(gt, preds)
    out["format_validity"] = round(1.0 if total == 0 else
                                   sum(len(v) for v in preds.values()) / total, 4)
    out["invalid_predictions"] = invalid
    if args.bootstrap > 0:
        out["map50_ci95"] = bootstrap_ci(gt, preds, args.bootstrap, args.seed)
    doc = json.dumps(out, indent=1, sort_keys=True)
    print(doc)
    if args.json:
        with open(args.json, "w") as fh:
            fh.write(doc + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
