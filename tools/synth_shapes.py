"""Deterministic synthetic shape-detection dataset -- the vision smoke test's corpus.

The vision analogue of the ARC line's synth_arc.py: every image is generated from a seed,
every box is exact by construction, and there is nothing to license and nobody to consent
-- which is precisely what a pipeline smoke test wants, because a smoke run measures the
PIPELINE, not the model. Ground truth that a generator computed is ground truth the mAP
gate can trust without a labeling-quality caveat.

    python tools/synth_shapes.py --out /tmp/vision-smoke --train 500 --eval 120 [--seed 42]

Layout per split (COCO): <out>/<split>/images/*.png + <out>/<split>/annotations.json.
Categories: circle(1), square(2), triangle(3). 1-4 shapes per 320x320 image, non-degenerate
boxes, colors and positions from the seeded RNG only -- rerunning with the same seed
reproduces every byte. No AWS calls: uploading the result is a separate, human-run step.
"""
import argparse
import json
import os
import random

CATEGORIES = [{"id": 1, "name": "circle"}, {"id": 2, "name": "square"},
              {"id": 3, "name": "triangle"}]
SIZE = 320


def _draw(rng, draw, kind, x, y, r, color):
    if kind == 1:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=color)
    elif kind == 2:
        draw.rectangle([x - r, y - r, x + r, y + r], fill=color)
    else:
        draw.polygon([(x, y - r), (x - r, y + r), (x + r, y + r)], fill=color)
    return [x - r, y - r, 2 * r, 2 * r]           # COCO [x, y, w, h]


def generate_split(out_dir, count, rng):
    from PIL import Image, ImageDraw
    os.makedirs(os.path.join(out_dir, "images"), exist_ok=True)
    images, annotations, ann_id = [], [], 1
    for i in range(count):
        img = Image.new("RGB", (SIZE, SIZE),
                        tuple(rng.randint(200, 255) for _ in range(3)))
        draw = ImageDraw.Draw(img)
        for _ in range(rng.randint(1, 4)):
            kind = rng.randint(1, 3)
            r = rng.randint(18, 55)
            x, y = rng.randint(r, SIZE - r), rng.randint(r, SIZE - r)
            color = tuple(rng.randint(0, 160) for _ in range(3))
            bbox = _draw(rng, draw, kind, x, y, r, color)
            annotations.append({"id": ann_id, "image_id": i, "category_id": kind,
                                "bbox": bbox, "area": bbox[2] * bbox[3], "iscrowd": 0})
            ann_id += 1
        name = f"{i:05d}.png"
        img.save(os.path.join(out_dir, "images", name))
        images.append({"id": i, "file_name": name, "width": SIZE, "height": SIZE})
    with open(os.path.join(out_dir, "annotations.json"), "w") as fh:
        json.dump({"images": images, "annotations": annotations,
                   "categories": CATEGORIES}, fh)
    return len(images), len(annotations)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--train", type=int, default=500)
    ap.add_argument("--eval", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)
    # one RNG, sequential splits: eval draws a disjoint stream from the same seed, so
    # train/eval cannot share an image and the decontamination scan's zero is honest
    rng = random.Random(args.seed)
    for split, count in (("train", args.train), ("eval", args.eval)):
        n_img, n_ann = generate_split(os.path.join(args.out, split), count, rng)
        print(f"{split}: {n_img} images, {n_ann} boxes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
