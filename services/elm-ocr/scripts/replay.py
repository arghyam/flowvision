"""
Re-score saved evaluation results under the current rules, or different thresholds, with no API calls (free).

evaluate.py stores the model's full answers. This feeds them back through the reader in place of the model, so a
change to the checks, the meter types or a threshold can be measured on the same photos without paying again.
Each photo is judged on its own, without history, as in evaluate.py.

    python scripts/replay.py data/evaluation.jsonl --images <photo folder>
    python scripts/replay.py data/evaluation.jsonl --images <photo folder> --clear-read-confidence 0.90
"""
import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))
from conf.config import Config, load_settings  # noqa: E402
from evaluate import outcome_of, result_record, summarize  # noqa: E402
from service.vision import MeterReader, ReaderSettings  # noqa: E402


class SavedAnswers:
    """Stands in for the model client (OpenAI or Claude): returns the answers saved for one photo, in order."""

    def __init__(self, saved):
        self.saved, self.calls = saved, 0

    def read(self, image_bytes, mime="image/jpeg", deadline=None):
        r = self.saved[self.calls % len(self.saved)]
        self.calls += 1
        return {"answer": r["answer"], "usage": r.get("usage") or {}, "model": r.get("model"),
                "seconds": r.get("seconds")}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("results", help="a JSONL file written by evaluate.py")
    ap.add_argument("--images", required=True, help="folder of the evaluated photos, named <image_id>.jpg")
    ap.add_argument("--min-confidence", type=float)
    ap.add_argument("--clear-read-confidence", type=float)
    args = ap.parse_args()

    try:
        settings = load_settings(Config())
    except RuntimeError:                      # no API key (OpenAI or Anthropic): not needed, nothing is sent
        settings = ReaderSettings(openai_api_key="replay")
    overrides = {k: v for k, v in (("min_confidence", args.min_confidence),
                                   ("clear_read_confidence", args.clear_read_confidence)) if v is not None}
    settings = replace(settings, **overrides)

    records = {}
    for line in Path(args.results).read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if rec["result"].get("answers"):
            records[rec["image_id"]] = rec            # the latest line per photo wins
    if not records:
        sys.exit("No saved answers in this file (it was written before evaluate.py stored them).")

    results, changed = [], []
    for iid, rec in records.items():
        saved = rec["result"]["answers"]
        reader = MeterReader(replace(settings, reads=len(saved)), client=SavedAnswers(saved))
        res = result_record(reader.read((Path(args.images) / f"{iid}.jpg").read_bytes()))
        res["costEstimateUsd"] = rec["result"].get("costEstimateUsd", 0)   # what the original run cost
        outcome = outcome_of(rec["truth_kind"], rec["truth"], res)
        results.append({**rec, "outcome": outcome, "result": res})
        if outcome != rec["outcome"]:
            changed.append((iid, rec["outcome"], outcome, res.get("candidate")))

    print(f"Replayed {len(results)} photos from {args.results} (no API calls)"
          + (f" with {overrides}" if overrides else ""))
    summarize(results)
    print(f"\n{len(changed)} outcome(s) differ from the original run:")
    for iid, before, after, cand in changed:
        print(f"  {iid:40s} {before} -> {after} ({cand or '-'})")


if __name__ == "__main__":
    main()
