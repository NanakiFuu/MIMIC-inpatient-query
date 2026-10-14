"""Render staircase-load curves: latency, errors and resources from measured data.

No simulation: every point comes from the recorded client samples or Prometheus
series. Writes evidence/p2/0b-load-curves/staircase-curves.png
"""
import gzip
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / 'evidence/p2/0b-load-curves'
BUCKET = 5.0


def series_points(series, start):
    points = []
    for item in series.get('result', []):
        for stamp, value in item.get('values', []):
            try:
                value = float(value)
            except ValueError:
                continue
            if math.isfinite(value):
                points.append((float(stamp) - start, value))
    return sorted(points)


def main():
    data = json.loads((DIR / 'staircase-load.json').read_text())
    start = data['overall_range']['start']
    rows = []
    with gzip.open(DIR / 'staircase-requests.jsonl.gz', 'rt', encoding='utf-8') as stream:
        for line in stream:
            rows.append(json.loads(line))
    buckets = {}
    for row in rows:
        key = int((row['utc_epoch'] - start) // BUCKET)
        buckets.setdefault(key, []).append(row)
    times = sorted(buckets)
    lat_p50, lat_p95, lat_p99, err_ratio = [], [], [], []
    for key in times:
        group = sorted(r['elapsed_ms'] for r in buckets[key])
        lat_p50.append(group[max(0, math.ceil(len(group) * .50) - 1)])
        lat_p95.append(group[max(0, math.ceil(len(group) * .95) - 1)])
        lat_p99.append(group[max(0, math.ceil(len(group) * .99) - 1)])
        err_ratio.append(sum(not r['correct'] for r in buckets[key]) / len(buckets[key]) * 100)
    xs = [key * BUCKET + BUCKET / 2 for key in times]

    fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True, layout='constrained')
    axes[0].plot(xs, lat_p50, label='client p50')
    axes[0].plot(xs, lat_p95, label='client p95')
    axes[0].plot(xs, lat_p99, label='client p99')
    axes[0].set_ylabel('Latency (ms)')
    axes[0].set_title('Fixed-API staircase load | client latency per 5s bucket')
    axes[0].legend()

    axes[1].plot(xs, err_ratio, color='#c0392b', label='error ratio (%)')
    axes[1].set_ylabel('Error ratio (%)')
    axes[1].set_title('Client error ratio per 5s bucket')
    axes[1].legend()

    for name, label in [('cpu_busy_ratio', 'CPU busy (fraction)'), ('server_p95_seconds', 'server p95 (s)')]:
        merged = []
        for run in data['runs']:
            merged.extend(series_points(run['series'][name], start))
        merged.sort()
        axes[2].plot([p[0] for p in merged], [p[1] for p in merged], label=label)
    axes[2].set_ylabel('Ratio / seconds')
    axes[2].set_xlabel('Seconds since experiment start (actual observations)')
    axes[2].set_title('Server-side observations')
    axes[2].legend()

    for run in data['runs']:
        x = run['start_utc_epoch'] - start
        for ax in axes:
            ax.axvline(x, color='gray', linestyle=':', linewidth=.8)
        axes[0].annotate(f"c={run['concurrency']}", (x, axes[0].get_ylim()[1]), fontsize=8, va='top')

    fig.savefig(DIR / 'staircase-curves.png', dpi=160)
    plt.close(fig)
    print('rendered', DIR / 'staircase-curves.png')


if __name__ == '__main__':
    main()
