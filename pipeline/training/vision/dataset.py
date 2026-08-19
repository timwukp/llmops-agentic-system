"""COCO-directory Dataset for the canonical detection trainer (sibling of
train_detection.py; travels in the same sourcedir).

Torch is imported lazily inside the class so this module stays importable -- and its
data-shaping helpers stay testable -- on machines without the training stack.
"""
import os


def coco_to_hf_annotations(image_id, records):
    """COCO annotation records -> the {image_id, annotations} dict HF processors eat."""
    return {"image_id": image_id,
            "annotations": [{"bbox": [float(v) for v in r["bbox"]],
                             "category_id": int(r["category_id"]),
                             "area": float(r.get("area") or
                                           r["bbox"][2] * r["bbox"][3]),
                             "iscrowd": int(r.get("iscrowd", 0))}
                            for r in records]}


class CocoDetectionDataset:
    def __init__(self, root, images, anns_by_image, processor):
        self.root = root
        self.images = images
        self.anns = anns_by_image
        self.processor = processor

    def __len__(self):
        return len(self.images)

    def __getitem__(self, idx):
        from PIL import Image
        meta = self.images[idx]
        img = Image.open(os.path.join(self.root, "images", meta["file_name"])).convert("RGB")
        target = coco_to_hf_annotations(meta["id"], self.anns.get(meta["id"], []))
        enc = self.processor(images=img, annotations=target, return_tensors="pt")
        return {"pixel_values": enc["pixel_values"][0], "labels": enc["labels"][0]}


def collate(batch):
    import torch
    return {"pixel_values": torch.stack([b["pixel_values"] for b in batch]),
            "labels": [b["labels"] for b in batch]}
