"""Measure UroScan's reading accuracy on a generated dataset.
Usage: python tools/evaluate.py dataset_folder
"""
import csv, os, sys
from collections import defaultdict
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from vision import analyse, ScanError  # noqa: E402
from reference import PAD_ORDER  # noqa: E402

folder = sys.argv[1] if len(sys.argv) > 1 else "dataset"
rows = list(csv.DictReader(open(os.path.join(folder, "labels.csv"))))
correct, within1, total, failed = defaultdict(int), defaultdict(int), defaultdict(int), 0
from reference import CHART  # noqa: E402
for row in rows:
    try:
        out = analyse(open(os.path.join(folder, "images", row["image"]), "rb").read())
    except ScanError as e:
        failed += 1
        print("FAILED", row["image"], e)
        continue
    for r in out["results"]:
        true_label = row[f"{r['analyte']}|level"]
        true_idx = [l[0] for l in CHART[r["analyte"]]].index(true_label)
        total[r["analyte"]] += 1
        correct[r["analyte"]] += r["level_index"] == true_idx
        within1[r["analyte"]] += abs(r["level_index"] - true_idx) <= 1
print(f"\nImages: {len(rows)}   could not read: {failed}")
print(f"{'Analyte':18s} exact   within-1-level")
for a in PAD_ORDER:
    if total[a]:
        print(f"{a:18s} {100*correct[a]/total[a]:5.1f}%   {100*within1[a]/total[a]:5.1f}%")
T = sum(total.values())
if T:
    print(f"{'OVERALL':18s} {100*sum(correct.values())/T:5.1f}%   {100*sum(within1.values())/T:5.1f}%")
