#!/usr/bin/env python3
"""metrics.py — 参赛工程的客观指标（只测量，不评价好坏）

评委可以拿这些当参考：它们描述"工程里发生了什么"，不判断"好不好听"。
指标全部从 .flp 的音符数据算出，不需要渲染音频。

用法：
    python3 tools/metrics.py submissions/<theme>/<id>/track.flp
    python3 tools/metrics.py submissions/*/*/track.flp --json > judging/metrics.json
    python3 tools/metrics.py submissions/*/*/track.flp --compare > judging/metrics.csv
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from flpkit import FLP  # noqa: E402


def analyse(path: Path) -> dict:
    proj = FLP.load(path)
    pats = proj.patterns()
    notes = [n for p in pats for n in p.notes]
    tpb = proj.ppq * 4
    bars = max((n.position + n.length for n in notes), default=0) / tpb
    onsets = [n.position for n in notes]

    per_bar = [0] * max(1, int(bars) + 1)
    for n in notes:
        per_bar[min(len(per_bar) - 1, int(n.position / tpb))] += 1

    # 同时发声数（最大/平均）
    ev = []
    for n in notes:
        ev.append((n.position, 1))
        ev.append((n.position + n.length, -1))
    ev.sort()
    cur = peak = 0
    area = 0
    last_t = 0
    for t, d in ev:
        area += cur * (t - last_t)
        last_t = t
        cur += d
        peak = max(peak, cur)
    span = max(last_t, 1)
    mean_poly = area / span

    pitches = [n.key for n in notes]
    vels = [n.velocity for n in notes]
    lens = [n.length for n in notes]
    pcs = len({k % 12 for k in pitches})
    oct_hist = {}
    for k in pitches:
        o = k // 12
        oct_hist[o] = oct_hist.get(o, 0) + 1

    ch_used: dict[int, int] = {}
    for n in notes:
        ch_used[n.rack_channel] = ch_used.get(n.rack_channel, 0) + 1

    # 结构曲线粗糙度：相邻小节音符数变化的平均值（低=平，高=段落对比强）
    diffs = [abs(per_bar[i + 1] - per_bar[i]) for i in range(len(per_bar) - 1)]
    roughness = statistics.mean(diffs) if diffs else 0.0

    names = proj.channel_names
    named_pat = sum(1 for p in pats if p.name and not p.name.startswith("Pattern"))

    return {
        "file": str(path),
        "technical": {
            "fl_version": proj.version,
            "ppq": proj.ppq,
            "channels": proj.channels,
            "patterns": len(pats),
            "notes": len(notes),
            "bpm": proj.tempo,
        },
        "time": {
            "bars": round(bars, 2),
            "seconds_est": round(bars * 4 * 60.0 / (proj.tempo or 120.0), 2),
        },
        "pitch": {
            "min": min(pitches) if pitches else None,
            "max": max(pitches) if pitches else None,
            "mean": round(statistics.mean(pitches), 2) if pitches else None,
            "distinct_pitch_classes": pcs,
            "octave_histogram": dict(sorted(oct_hist.items())),
        },
        "rhythm": {
            "density_mean_per_bar": round(statistics.mean(per_bar), 2),
            "density_std": round(statistics.pstdev(per_bar), 2),
            "silent_bars": sum(1 for x in per_bar if x == 0),
            "short_note_ratio": round(sum(1 for l in lens if l < tpb / 4) / len(lens), 3) if lens else 0,
            "distinct_onset_ticks": len(set(onsets)),
        },
        "polyphony": {"max": peak, "mean": round(mean_poly, 2)},
        "velocity": {
            "mean": round(statistics.mean(vels), 2) if vels else None,
            "std": round(statistics.pstdev(vels), 2) if vels else None,
            "min": min(vels) if vels else None,
            "max": max(vels) if vels else None,
        },
        "structure": {
            "bar_note_curve": per_bar,
            "curve_roughness": round(roughness, 2),
        },
        "channels": {
            "used": dict(sorted(ch_used.items())),
            "named_channels": len(names),
            "named_patterns": named_pat,
        },
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="FLP 客观指标")
    ap.add_argument("flp", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--compare", action="store_true")
    a = ap.parse_args(argv)
    rows = [analyse(Path(p)) for p in a.flp]
    if a.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    elif a.compare:
        print("file,notes,bars,sec_est,bpm,channels,patterns,density,roughness,poly_max,vel_std,pitch_classes")
        for r in rows:
            print(f"{r['file']},{r['technical']['notes']},{r['time']['bars']},"
                  f"{r['time']['seconds_est']},{r['technical']['bpm']},"
                  f"{r['technical']['channels']},{r['technical']['patterns']},"
                  f"{r['rhythm']['density_mean_per_bar']},{r['structure']['curve_roughness']},"
                  f"{r['polyphony']['max']},{r['velocity']['std']},"
                  f"{r['pitch']['distinct_pitch_classes']}")
    else:
        for r in rows:
            t, tm, p, rh = r["technical"], r["time"], r["pitch"], r["rhythm"]
            print(f"\n=== {r['file']} ===")
            print(f"  FL {t['fl_version']} | PPQ {t['ppq']} | {t['channels']} 通道 | "
                  f"{t['patterns']} pattern | {t['notes']} 音符 | {tm['bars']} 小节 ≈ {tm['seconds_est']}s | {t['bpm']} BPM")
            print(f"  音高 {p['min']}..{p['max']}（均值 {p['mean']}，{p['distinct_pitch_classes']} 个音级）")
            print(f"  节奏 密度 {rh['density_mean_per_bar']}/小节（σ {rh['density_std']}），"
                  f"空小节 {rh['silent_bars']}，短音比例 {rh['short_note_ratio']}")
            print(f"  织体 最大同时 {r['polyphony']['max']} 音，均值 {r['polyphony']['mean']}")
            print(f"  力度 均值 {r['velocity']['mean']}（σ {r['velocity']['std']}，范围 "
                  f"{r['velocity']['min']}..{r['velocity']['max']}）")
            print(f"  结构 相邻小节起伏 {r['structure']['curve_roughness']}，"
                  f"命名通道 {r['channels']['named_channels']}，命名 pattern {r['channels']['named_patterns']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
