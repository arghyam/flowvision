"""
Fine-tune Color Classification Model
======================================
Fine-tunes the active color_classification_fastai_v2 model on newly annotated
last-digit color crops to improve red/black classification.

Data layout expected under src/data/:
  src/data/training/color/
    red/    ← red digit crop images
    black/  ← black digit crop images

  src/data/test/color/
    red/    ← held-out red crops (from annotate_1076)
    black/  ← held-out black crops (from annotate_1076)
    annotations.csv  ← must contain 'source_stem', 'label', and optionally
                        'pipeline_pred' columns

Class imbalance is handled with inverse-frequency weighted cross-entropy
so the model does not collapse to always predicting the majority class.

Training strategy:
  Phase 1 — freeze backbone, train head only    (10 epochs)
  Phase 2 — unfreeze all layers, low LR         (15 epochs)

Each run is stamped with a unique RUN_ID. Outputs:
  training/color/models/color_{RUN_ID}_phase1.pth   intermediate checkpoint
  training/color/models/color_{RUN_ID}_phase2.pth   intermediate checkpoint
  training/color/training_results_{RUN_ID}.csv      per-epoch metrics
  src/models/color_{RUN_ID}                         exported FastAI learner

To deploy a run, update src/conf/config.yaml:
  models:
    color_classification: "src/models/color_{RUN_ID}"

Run from project root:
  python training/color/finetune_color.py
"""

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "src"))

from pathlib import Path
from datetime import datetime

import pandas as pd
import torch

from fastai.vision.all import (
    get_image_files,
    ImageBlock, CategoryBlock, DataBlock,
    RandomSplitter, parent_label, Resize,
    aug_transforms, vision_learner,
    error_rate, load_learner, PILImage,
    CrossEntropyLossFlat,
    ClassificationInterpretation,
    resnet18,
)

RUN_ID = datetime.now().strftime("%Y%m%d_%H%M%S")

# ── Paths ──────────────────────────────────────────────────────────────────────

TRAIN_CROPS_DIR = Path("src/data/training/color")
TEST_CROPS_DIR  = Path("src/data/test/color")
TEST_ANN_CSV    = Path("src/data/test/color/annotations.csv")
ORIG_MODEL      = Path("src/models/color_classification_fastai_v2")
CKPT_DIR        = Path("training/color/models")
EXPORT_DIR      = Path("src/models")

CKPT_DIR.mkdir(exist_ok=True)

# ── Hyperparameters ────────────────────────────────────────────────────────────

IMG_SIZE      = 224
VALID_PCT     = 0.2
SEED          = 42
PHASE1_EPOCHS = 10
PHASE2_EPOCHS = 15
PHASE2_LR     = slice(1e-6, 3e-4)
BATCH_SIZE    = 16    # small dataset — keep batch size modest


# ── Helpers ────────────────────────────────────────────────────────────────────

def print_section(title):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── Run banner ────────────────────────────────────────────────────────────────

print(f"\n{'═'*60}")
print(f"  COLOR CLASSIFIER FINE-TUNE — RUN ID: {RUN_ID}")
print(f"{'═'*60}")


# ── Step 1: Inspect existing model ────────────────────────────────────────────

print_section("Step 1 — Inspecting existing model")
existing_learn = load_learner(ORIG_MODEL)
old_vocab      = list(existing_learn.dls.vocab)
print(f"  Vocab (existing model):  {old_vocab}")
total_params = sum(p.numel() for p in existing_learn.model.parameters())
print(f"  Total parameters:        {total_params:,}")

arch_fn   = resnet18
arch_name = "resnet18"
if hasattr(existing_learn, "arch") and existing_learn.arch is not None:
    try:
        arch_fn   = existing_learn.arch
        arch_name = existing_learn.arch.__name__
    except Exception:
        pass
print(f"  Architecture:            {arch_name}")


# ── Step 2: Count training crops ──────────────────────────────────────────────

print_section("Step 2 — Collecting training crops")

red_n   = len(list((TRAIN_CROPS_DIR / "red").glob("*.jpg")))
black_n = len(list((TRAIN_CROPS_DIR / "black").glob("*.jpg")))
total_n = red_n + black_n
print(f"  training/color/red:   {red_n}")
print(f"  training/color/black: {black_n}")
print(f"  Total:                {total_n}")

if total_n == 0:
    print("\nERROR: No training crops found in src/data/training/color/")
    sys.exit(1)

new_classes = sorted([d.name for d in TRAIN_CROPS_DIR.iterdir()
                      if d.is_dir() and any(d.glob("*.jpg"))])
print(f"  Classes detected: {new_classes}")
if sorted(old_vocab) != sorted(new_classes):
    print(f"  WARNING: Vocab mismatch — existing: {old_vocab}, new data: {new_classes}")


# ── Step 3: Compute class weights ─────────────────────────────────────────────

print_section("Step 3 — Computing class weights for imbalanced data")

label_counts  = {"red": red_n, "black": black_n}
classes_sorted = sorted(label_counts.keys())
weights = torch.tensor(
    [total_n / (len(classes_sorted) * label_counts.get(c, 1)) for c in classes_sorted],
    dtype=torch.float32,
)
print(f"  Distribution: {label_counts}")
print(f"  Weights ({classes_sorted}): {weights.tolist()}")
loss_func = CrossEntropyLossFlat(weight=weights)


# ── Step 4: Build DataLoaders ─────────────────────────────────────────────────

print_section("Step 4 — Building DataLoaders")

color_block = DataBlock(
    blocks     = (ImageBlock, CategoryBlock),
    get_items  = get_image_files,
    splitter   = RandomSplitter(valid_pct=VALID_PCT, seed=SEED),
    get_y      = parent_label,
    item_tfms  = Resize(IMG_SIZE),
    # Flipping and rotation are label-preserving for color identity —
    # a red digit is still red when flipped or slightly rotated.
    batch_tfms = aug_transforms(
        do_flip      = True,
        max_rotate   = 15.0,
        max_zoom     = 1.1,
        max_warp     = 0.0,
        max_lighting = 0.3,
        p_lighting   = 0.75,
    ),
)
dls = color_block.dataloaders(TRAIN_CROPS_DIR, bs=BATCH_SIZE)
print(f"  Train batches: {len(dls.train)}  |  Valid batches: {len(dls.valid)}")
print(f"  Vocab: {dls.vocab}")


# ── Step 5: Load existing weights ─────────────────────────────────────────────

print_section("Step 5 — Loading existing weights into new learner")

learn = vision_learner(dls, arch_fn, metrics=error_rate, loss_func=loss_func)

if list(dls.vocab) == old_vocab:
    try:
        learn.model.load_state_dict(existing_learn.model.state_dict(), strict=False)
        print("  Weights loaded from existing model (strict=False).")
    except Exception as e:
        print(f"  WARNING: Could not copy weights ({e}) — using ImageNet weights.")
else:
    # Vocab order changed — copy only backbone layers with matching shape
    print(f"  Vocab changed ({old_vocab} → {list(dls.vocab)}) — copying backbone only.")
    try:
        old_state  = existing_learn.model.state_dict()
        new_state  = learn.model.state_dict()
        compatible = {k: v for k, v in old_state.items()
                      if k in new_state and v.shape == new_state[k].shape}
        new_state.update(compatible)
        learn.model.load_state_dict(new_state)
        print(f"  Copied {len(compatible)}/{len(new_state)} parameter tensors.")
    except Exception as e:
        print(f"  Could not copy backbone weights ({e}) — using ImageNet weights.")

del existing_learn   # free GPU memory


# ── Step 6: LR finder ─────────────────────────────────────────────────────────

print_section("Step 6 — Learning rate finder")
try:
    lr_result = learn.lr_find()
    PHASE1_LR = lr_result.valley
    print(f"  Suggested LR (valley): {PHASE1_LR:.2e}")
except Exception as e:
    PHASE1_LR = 1e-3
    print(f"  LR finder failed ({type(e).__name__}) — falling back to {PHASE1_LR:.2e}")


# ── Step 7: Phase 1 — Head only ───────────────────────────────────────────────

print_section(f"Step 7 — Phase 1: head-only training ({PHASE1_EPOCHS} epochs)")
learn.freeze()
learn.fit_one_cycle(PHASE1_EPOCHS, PHASE1_LR)
ckpt1 = CKPT_DIR / f"color_{RUN_ID}_phase1"
learn.save(str(ckpt1))
print(f"  Checkpoint saved → {ckpt1}.pth")


# ── Step 8: Phase 2 — Full fine-tune ──────────────────────────────────────────

print_section(f"Step 8 — Phase 2: full fine-tune ({PHASE2_EPOCHS} epochs)")
learn.unfreeze()
learn.fit_one_cycle(PHASE2_EPOCHS, lr_max=PHASE2_LR)
ckpt2 = CKPT_DIR / f"color_{RUN_ID}_phase2"
learn.save(str(ckpt2))
print(f"  Checkpoint saved → {ckpt2}.pth")


# ── Step 9: Training metrics ───────────────────────────────────────────────────

print_section("Step 9 — Saving training metrics")
metrics_rows = []
for epoch, vals in enumerate(learn.recorder.values):
    metrics_rows.append({
        "epoch":      epoch + 1,
        "valid_loss": vals[0],
        "error_rate": vals[1],
        "accuracy":   1.0 - vals[1],
    })
metrics_df   = pd.DataFrame(metrics_rows)
metrics_path = Path(__file__).parent / f"training_results_{RUN_ID}.csv"
metrics_df.to_csv(metrics_path, index=False)
print(f"  Saved to {metrics_path}")
print(metrics_df.to_string(index=False))


# ── Step 10: Validation confusion matrix ──────────────────────────────────────

print_section("Step 10 — Validation set confusion matrix")
interp = ClassificationInterpretation.from_learner(learn)
interp.print_classification_report()


# ── Step 11: Evaluate on held-out test crops ──────────────────────────────────

print_section("Step 11 — Final test evaluation on src/data/test/color/")

if TEST_ANN_CSV.exists():
    gt_df = pd.read_csv(TEST_ANN_CSV)
    gt_df = gt_df[gt_df["label"].isin(["red", "black"])].copy()
    print(f"  Ground truth rows: {len(gt_df)}  "
          f"(red: {(gt_df['label']=='red').sum()}, black: {(gt_df['label']=='black').sum()})")

    correct = tp_red = fp_red = fn_red = tn_red = tested = 0

    for _, row in gt_df.iterrows():
        stem  = str(row["source_stem"])
        label = str(row["label"])
        path  = TEST_CROPS_DIR / label / f"{stem}_last.jpg"
        if not path.exists():
            continue
        try:
            pred_class, _, _ = learn.predict(PILImage.create(path))
            pred  = str(pred_class).lower()
            truth = label
            tested += 1
            if pred == truth:
                correct += 1
            if truth == "red":
                if pred == "red": tp_red += 1
                else:             fn_red += 1
            else:
                if pred == "red": fp_red += 1
                else:             tn_red += 1
        except Exception:
            pass

    if tested > 0:
        accuracy   = correct / tested
        red_recall = tp_red / (tp_red + fn_red) if (tp_red + fn_red) > 0 else 0
        black_prec = tn_red / (tn_red + fp_red) if (tn_red + fp_red) > 0 else 0
        print(f"  Images tested:     {tested} / {len(gt_df)}")
        print(f"  Overall accuracy:  {accuracy*100:.1f}%")
        print(f"  Red recall:        {red_recall*100:.1f}%  ({tp_red} caught, {fn_red} missed)")
        print(f"  Black precision:   {black_prec*100:.1f}%  ({tn_red} correct, {fp_red} false alarms)")

        if "pipeline_pred" in gt_df.columns:
            cmp = gt_df[["pipeline_pred", "label"]].dropna()
            if len(cmp):
                baseline = (cmp["pipeline_pred"] == cmp["label"]).mean()
                print(f"\n  Pipeline baseline: {baseline*100:.1f}%  "
                      f"vs fine-tuned: {accuracy*100:.1f}%")
    else:
        print("  No crop files found on disk — populate src/data/test/color/ first.")
else:
    print(f"  {TEST_ANN_CSV} not found — add annotations.csv to src/data/test/color/")


# ── Step 12: Export model ──────────────────────────────────────────────────────

print_section("Step 12 — Exporting model")
export_path = EXPORT_DIR / f"color_{RUN_ID}"
learn.export(export_path)
print(f"  Exported to {export_path}")
print(f"\nTo deploy, update src/conf/config.yaml:")
print(f"  models:")
print(f"    color_classification: \"src/models/color_{RUN_ID}\"")
print(f"\nRUN ID: {RUN_ID}")
