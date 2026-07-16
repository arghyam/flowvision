"""
Fine-tune BFM Quality Classifier
==================================
Fine-tunes the active bfm_fastai_v2 model on new labelled images to improve
meter-readability (good/bad) classification.

Data layout expected under src/data/:
  src/data/training/quality/
    good/          ← training images labelled good
    bad/           ← training images labelled bad
    annotations.csv  ← optional; must contain 'image' and 'verified' columns
                       if present; only verified=True rows are used
  src/data/test/
    images/        ← held-out test images (never used for training)
    annotations.csv  ← must contain 'image' and 'image_quality' columns

Training strategy (two-phase fine-tune):
  Phase 1 — freeze backbone, train FastAI head only   (5 epochs)
  Phase 2 — unfreeze all layers, train end-to-end     (10 epochs)

Each run is stamped with a RUN_ID (YYYYMMDD_HHMMSS). Outputs:
  training/quality/models/bfm_{RUN_ID}_phase1.pth   intermediate checkpoint
  training/quality/models/bfm_{RUN_ID}_phase2.pth   intermediate checkpoint
  training/quality/training_results_{RUN_ID}.csv    per-epoch metrics
  src/models/bfm_{RUN_ID}                           exported FastAI learner

To deploy a run, update config.yaml:
  models:
    bfm_classification: "src/models/bfm_{RUN_ID}"

Run from project root:
  python training/quality/finetune_bfm.py
"""

import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from pathlib import Path
import pandas as pd
from datetime import datetime

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

from fastai.vision.all import (
    get_image_files, ImageBlock, CategoryBlock, DataBlock,
    RandomSplitter, parent_label, Resize, RandomResizedCrop,
    aug_transforms, vision_learner, resnet50, error_rate,
    ClassificationInterpretation, load_learner, PILImage,
)

# ── Paths ──────────────────────────────────────────────────────────────────────

PROJECT_ROOT  = Path(__file__).parent.parent.parent
TRAIN_IMG_DIR = PROJECT_ROOT / "src" / "data" / "training" / "quality"
TRAIN_ANN_CSV = PROJECT_ROOT / "src" / "data" / "training" / "quality" / "annotations.csv"
TEST_ANN_CSV  = PROJECT_ROOT / "src" / "data" / "test" / "annotations.csv"
TEST_IMG_DIR  = PROJECT_ROOT / "src" / "data" / "test" / "images"
ORIG_MODEL    = PROJECT_ROOT / "src" / "models" / "bfm_fastai_v2"
CKPT_DIR      = PROJECT_ROOT / "training" / "quality" / "models"
EXPORT_DIR    = PROJECT_ROOT / "src" / "models"

CKPT_DIR.mkdir(exist_ok=True)

# ── Hyperparameters ────────────────────────────────────────────────────────────

IMG_SIZE      = 224
VALID_PCT     = 0.2
SEED          = 42
PHASE1_EPOCHS = 5
PHASE2_EPOCHS = 10
PHASE2_LR     = slice(1e-6, 1e-4)


# ── Helpers ────────────────────────────────────────────────────────────────────

def print_section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── Run info ───────────────────────────────────────────────────────────────────

print(f"\n{'═'*60}")
print(f"  RUN ID: {RUN_ID}")
print(f"{'═'*60}")


# ── Step 1: Inspect existing model ────────────────────────────────────────────

print_section("Step 1 — Inspecting existing model")
existing_learn = load_learner(ORIG_MODEL)
print(f"  Classes (vocab): {existing_learn.dls.vocab}")
total_params = sum(p.numel() for p in existing_learn.model.parameters())
print(f"  Total parameters: {total_params:,}")


# ── Step 2: Build DataLoaders ─────────────────────────────────────────────────

print_section("Step 2 — Building DataLoaders")

verified_stems = None
if TRAIN_ANN_CSV.exists():
    ann_df = pd.read_csv(TRAIN_ANN_CSV)
    print(f"  Annotations loaded: {len(ann_df)} rows")
    if "verified" in ann_df.columns:
        unverified = (~ann_df["verified"].astype(bool)).sum()
        ann_df = ann_df[ann_df["verified"].astype(bool)].copy()
        print(f"  Unverified rows excluded: {unverified} — using {len(ann_df)} verified rows")
        if len(ann_df) == 0:
            print("\nERROR: No verified annotations found. Label images before finetuning.")
            sys.exit(1)
    verified_stems = set(ann_df["image"].astype(str))

def get_training_images(path):
    files = get_image_files(path)
    if verified_stems is not None:
        files = [f for f in files if f.stem in verified_stems]
    return files

good_count = len(list((TRAIN_IMG_DIR / "good").glob("*.jpg")))
bad_count  = len(list((TRAIN_IMG_DIR / "bad").glob("*.jpg")))
print(f"  Training images on disk — good: {good_count}, bad: {bad_count}")

if good_count + bad_count == 0:
    print("\nERROR: No training images found in src/data/training/quality/")
    sys.exit(1)

# Only brightness/contrast augmentation — spatial transforms disabled because
# flipping or rotating a meter image can change its readability label.
quality_block = DataBlock(
    blocks     = (ImageBlock, CategoryBlock),
    get_items  = get_training_images,
    splitter   = RandomSplitter(valid_pct=VALID_PCT, seed=SEED),
    get_y      = parent_label,
    item_tfms  = Resize(IMG_SIZE),
    batch_tfms = aug_transforms(
        do_flip=False, max_rotate=0.0, max_zoom=1.0,
        max_warp=0.0, max_lighting=0.2,
    ),
)
dls = quality_block.dataloaders(TRAIN_IMG_DIR)
print(f"  Train batches: {len(dls.train)}  |  Valid batches: {len(dls.valid)}")
print(f"  Classes: {dls.vocab}")


# ── Step 3: Load existing weights ─────────────────────────────────────────────

print_section("Step 3 — Loading existing weights into new learner")
learn = vision_learner(dls, resnet50, metrics=error_rate)
learn.model.load_state_dict(existing_learn.model.state_dict())
print("  Weights loaded from bfm_fastai_v2.")


# ── Step 4: LR finder ─────────────────────────────────────────────────────────

print_section("Step 4 — Learning rate finder")
lr_result  = learn.lr_find()
PHASE1_LR  = lr_result.valley
print(f"  Suggested LR (valley): {PHASE1_LR:.2e}")


# ── Step 5: Phase 1 — Head only ───────────────────────────────────────────────

print_section(f"Step 5 — Phase 1: head-only training ({PHASE1_EPOCHS} epochs)")
learn.freeze()
learn.fit_one_cycle(PHASE1_EPOCHS, PHASE1_LR)
ckpt1 = CKPT_DIR / f"bfm_{RUN_ID}_phase1"
learn.save(str(ckpt1))
print(f"  Checkpoint saved → {ckpt1}.pth")


# ── Step 6: Phase 2 — Full fine-tune ──────────────────────────────────────────

print_section(f"Step 6 — Phase 2: full fine-tune ({PHASE2_EPOCHS} epochs)")
learn.unfreeze()
learn.fit_one_cycle(PHASE2_EPOCHS, lr_max=PHASE2_LR)
ckpt2 = CKPT_DIR / f"bfm_{RUN_ID}_phase2"
learn.save(str(ckpt2))
print(f"  Checkpoint saved → {ckpt2}.pth")


# ── Step 7: Training metrics ───────────────────────────────────────────────────

print_section("Step 7 — Saving training metrics")
metrics_rows = []
for epoch, vals in enumerate(learn.recorder.values):
    metrics_rows.append({
        "epoch":      epoch + 1,
        "train_loss": learn.recorder.losses[epoch] if epoch < len(learn.recorder.losses) else None,
        "valid_loss": vals[0],
        "error_rate": vals[1],
        "accuracy":   1 - vals[1],
    })
metrics_df   = pd.DataFrame(metrics_rows)
metrics_path = Path(__file__).parent / f"training_results_{RUN_ID}.csv"
metrics_df.to_csv(metrics_path, index=False)
print(f"  Saved to {metrics_path}")
print(metrics_df.to_string(index=False))


# ── Step 8: Validation confusion matrix ───────────────────────────────────────

print_section("Step 8 — Validation set confusion matrix")
interp = ClassificationInterpretation.from_learner(learn)
interp.print_classification_report()


# ── Step 9: Evaluate on held-out test set ─────────────────────────────────────

print_section("Step 9 — Evaluation on held-out test set (src/data/test/)")

if TEST_ANN_CSV.exists():
    test_ann = pd.read_csv(TEST_ANN_CSV)
    test_ann = test_ann[test_ann["image_quality"].isin(["good", "bad"])].copy()

    correct = total_tested = tp_bad = fp_bad = fn_bad = tn_bad = 0

    for _, row in test_ann.iterrows():
        path  = TEST_IMG_DIR / (str(row["image"]) + ".jpg")
        truth = row["image_quality"]
        if not path.exists():
            continue
        try:
            _, _, probs = learn.predict(PILImage.create(path))
            all_probs = [float(p) for p in probs]
            good_idx  = list(learn.dls.vocab).index("good")
            good_prob = all_probs[good_idx]
            pred      = "good" if good_prob >= 0.65 else "bad"
            total_tested += 1
            if pred == truth:
                correct += 1
            if truth == "bad":
                if pred == "bad": tp_bad += 1
                else:             fn_bad += 1
            else:
                if pred == "bad": fp_bad += 1
                else:             tn_bad += 1
        except Exception:
            pass

    if total_tested > 0:
        print(f"  Images tested:    {total_tested} / {len(test_ann)}")
        print(f"  Overall accuracy: {correct/total_tested*100:.1f}%")
        print(f"  Bad recall:       {tp_bad/(tp_bad+fn_bad)*100:.1f}%  ({tp_bad} caught, {fn_bad} missed)")
        print(f"  Good pass rate:   {tn_bad/(tn_bad+fp_bad)*100:.1f}%  ({tn_bad} passed, {fp_bad} rejected)")
    else:
        print("  No test images found — skipping.")
else:
    print(f"  Test annotations not found at {TEST_ANN_CSV} — skipping.")


# ── Step 10: Export ────────────────────────────────────────────────────────────

print_section("Step 10 — Exporting model")
export_path = EXPORT_DIR / f"bfm_{RUN_ID}"
learn.export(export_path)
print(f"  Exported to {export_path}")
print(f"\nTo deploy, update src/conf/config.yaml:")
print(f"  models:")
print(f"    bfm_classification: \"src/models/bfm_{RUN_ID}\"")
print(f"\nRUN ID: {RUN_ID}")
