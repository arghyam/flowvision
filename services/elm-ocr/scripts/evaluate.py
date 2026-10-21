"""
Run the full meter reader (real OpenAI calls) over hand-checked photos and report how safe it is.

Needs a review CSV with columns image_id, truth_kind (reading | not_cumulative | unreadable), truth_display,
and a folder of photos named <image_id>.jpg. By default: the trial's hand review (../trial/review.csv) and its
photos (../trial/outputs/review_images/, local only).

Costs money: about $0.023 per photo per read with GPT-6 Sol (so ~$2.30 for 100 photos at the default METER_READS=1).
Uses the provider and model in config.yaml / METER_PROVIDER; Claude's cost per photo has not been measured yet.
It refuses to run without --yes.

Results are appended to --out, one line per photo with the model's full answer (so rule changes can be replayed
offline). Re-running skips photos already answered with the same prompt version, so a crash costs nothing twice;
--fresh starts over.

    python scripts/evaluate.py --yes
    python scripts/evaluate.py --yes --limit 10 --reads 1
"""
import argparse
import csv
import json
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from conf.config import Config, load_settings  # noqa: E402
from service.vision import ACCEPTED, MeterReader  # noqa: E402
from service.vision.prompt import PROMPT_VERSION  # noqa: E402

TRIAL = HERE.parent.parent / "trial" / "outputs"


def to_value(display):
    try:
        return Decimal("".join(ch for ch in display if ch.isdigit() or ch == ".")) if display else None
    except InvalidOperation:
        return None


def result_record(r):
    """The stored form of one ReadingResult (used by evaluate.py and replay.py)."""
    return {"status": r.status, "meterReading": r.reading if r.status == ACCEPTED else None,
            "candidate": r.reading, "candidateDecimalSource": r.decimal_source,
            "uncertainDigits": r.uncertain_digits, "reasons": r.reason_codes, "meterType": r.meter.get("typeId"),
            "reads": r.reads, "costEstimateUsd": r.cost_usd, "answers": r.raw_answers}


def outcome_of(truth_kind, truth, res):
    right = lambda v: truth_kind == "reading" and v is not None and to_value(v) == to_value(truth)
    if res["status"] == ACCEPTED:
        return "correct" if right(res["meterReading"]) else "FALSE_POSITIVE"
    if res.get("reasons") == ["NO_CROSS_CHECK"]:
        # held only because this run has no reading history; with a previous reading it could have been accepted
        return "no_history_right" if right(res["candidate"]) else "no_history_WRONG"
    return res["status"].lower() + ("" if truth_kind == "reading" else "_ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--review", default=str(TRIAL.parent / "review.csv"))
    ap.add_argument("--images", default=str(TRIAL / "review_images"))
    ap.add_argument("--out", default=str(HERE.parent / "data" / "evaluation.jsonl"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--reads", type=int)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--yes", action="store_true", help="confirm you accept the API cost")
    ap.add_argument("--fresh", action="store_true", help="ignore earlier results in --out and start over")
    args = ap.parse_args()

    with open(args.review, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))[: args.limit]
    settings = load_settings(Config())        # no store: every photo is judged on its own, without history
    if args.reads:
        settings = replace(settings, reads=args.reads)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {}
    if out.exists() and not args.fresh:
        for line in out.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            # records written before the model was stored all came from GPT-6 Sol
            if rec["result"].get("status") != "ERROR" and rec.get("promptVersion") == PROMPT_VERSION \
                    and rec.get("reads") == settings.reads and rec.get("model", "gpt-6-sol") == settings.model:
                done[rec["image_id"]] = rec
    todo = [r for r in rows if r["image_id"] not in done]
    print(f"{len(todo)} photos to read ({len(rows) - len(todo)} already done) x {settings.reads} read(s) "
          f"with {settings.model}: " + (f"about ${len(todo) * settings.reads * 0.023:.2f}" if settings.provider == "openai"
                                        else "cost per photo not yet measured (GPT-6 Sol: about $0.023)"))
    if not args.yes:
        sys.exit("Add --yes to run it.")
    reader = MeterReader(settings)

    def job(row):
        img = Path(args.images) / f"{row['image_id']}.jpg"
        try:
            res = result_record(reader.read(img.read_bytes()))
        except Exception as e:  # noqa: BLE001 - a failed call can still be billed: keep its cost
            res = {"status": "ERROR", "error": str(e), "costEstimateUsd": float(getattr(e, "cost_usd", 0.0) or 0.0)}
        return {"image_id": row["image_id"], "truth_kind": row["truth_kind"], "truth": row.get("truth_display", ""),
                "outcome": outcome_of(row["truth_kind"], row.get("truth_display", ""), res),
                "promptVersion": PROMPT_VERSION, "model": settings.model, "reads": settings.reads, "result": res}

    t0 = time.time()
    new = []
    with ThreadPoolExecutor(args.workers) as ex, out.open("w" if args.fresh else "a", encoding="utf-8") as f:
        for i, fut in enumerate(as_completed([ex.submit(job, r) for r in todo]), 1):
            r = fut.result()
            new.append(r)
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{i}/{len(todo)}] {r['image_id']:40s} {r['outcome']:16s} {r['result'].get('meterReading') or ''}",
                  flush=True)
    by_id = {**done, **{r["image_id"]: r for r in new}}
    results = [by_id[r["image_id"]] for r in rows if r["image_id"] in by_id]

    summarize(results)
    print(f"{time.time() - t0:.0f}s; details in {out}")


def summarize(results):
    c = Counter(r["outcome"] for r in results)
    accepted = c["correct"] + c["FALSE_POSITIVE"]
    readable = sum(r["truth_kind"] == "reading" for r in results)
    cost = sum(r["result"].get("costEstimateUsd", 0) for r in results)
    print("\n== Summary ==")
    print(f"photos: {len(results)} ({readable} readable by the hand review)")
    # meterReading is returned whatever the status: how often is it right, decimal point included?
    shown = [r for r in results if r["truth_kind"] == "reading" and r["result"].get("status") != "ERROR"]
    shown_right = [r for r in shown if to_value(r["result"].get("candidate")) is not None
                   and to_value(r["result"]["candidate"]) == to_value(r["truth"])]
    print(f"reading returned right (any status, decimal point included): {len(shown_right)}/{len(shown)} readable photos")
    print(f"accepted readings: {accepted}  correct: {c['correct']}  FALSE POSITIVES: {c['FALSE_POSITIVE']}")
    if accepted:
        print(f"precision: {100 * c['correct'] / accepted:.1f}%   coverage of readable photos: "
              f"{100 * c['correct'] / max(1, readable):.1f}% ({c['correct']}/{readable})")
    held = c["no_history_right"] + c["no_history_WRONG"]
    if held:
        print(f"held for review only because there is no previous reading (NO_CROSS_CHECK): {held}, "
              f"candidate right {c['no_history_right']}, WRONG {c['no_history_WRONG']} "
              f"(wrong ones must be caught by the previous-reading check in production)")
    print(f"other outcomes: {dict((k, v) for k, v in c.items() if k not in ('correct', 'FALSE_POSITIVE', 'no_history_right', 'no_history_WRONG'))}")

    # The best guess returned with every retake/review, on readable photos that were not accepted
    held = [r for r in results if r["truth_kind"] == "reading" and r["outcome"] not in ("correct", "FALSE_POSITIVE")
            and r["result"].get("status") != "ERROR"]
    if held:
        digits = lambda s: "".join(ch for ch in (s or "") if ch.isdigit()).lstrip("0")
        exact = [r for r in held if r["result"].get("candidate") and "?" not in r["result"]["candidate"]
                 and to_value(r["result"]["candidate"]) == to_value(r["truth"])]
        point = [r for r in held if r not in exact and r["result"].get("candidate")
                 and digits(r["result"]["candidate"]) == digits(r["truth"])]
        print(f"best guess on the {len(held)} readable photos not accepted: value right {len(exact)}, "
              f"digits right but decimal point wrong {len(point)}, other {len(held) - len(exact) - len(point)}")
        for r in held:
            res = r["result"]
            print(f"  {r['image_id']:40s} truth {r['truth']:11s} guess {res.get('candidate') or '-':11s} "
                  f"decimal from {res.get('candidateDecimalSource') or '-':16s} unsure digits {res.get('uncertainDigits')}")
    print(f"API cost of these results about ${cost:.2f}")
    for r in results:
        if r["outcome"] == "FALSE_POSITIVE":
            print(f"  FP {r['image_id']}: truth {r['truth'] or r['truth_kind']}, accepted {r['result']['meterReading']}")
        elif r["outcome"] == "no_history_WRONG":
            print(f"  held, wrong candidate {r['image_id']}: truth {r['truth'] or r['truth_kind']}, "
                  f"candidate {r['result']['candidate']}")


if __name__ == "__main__":
    main()
