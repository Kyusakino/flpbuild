#!/usr/bin/env python3
"""check_audio.py — 音乐 AI 组（music-ai）音频作品校验

music-ai 组的模型通常只产出音频（不是 .flp），所以走这条通道：
    * 文件能解码（wav / flac / mp3 / aiff，取决于 libsndfile）
    * 时长 90–240 秒
    * 不是静音（整体 RMS > -60 dBFS）
    * 采样率 ≥ 32000 Hz、≤ 2 声道

用法：
    python3 tools/check_audio.py submissions/theme-a/music-ai/flowmusic/track.wav --expect-bpm 90
    python3 tools/check_audio.py submissions/theme-b/music-ai/minimax-music/track.wav \
        --expect-bpm 180 --bpm-tolerance 20
    python3 tools/check_audio.py submissions/theme-*/music-ai/*/track.wav --json > judging/report-audio.json

BPM 校验说明：本地做粗略估算（谱通量 + 梳状自相关），只作为提示：
  * 与主题要求差 ≤ 容差 → OK
  * 差 ≤ 容差+8 → WARN（需人工确认；半速/倍速会同时尝试）
  * 差 > 容差+8 → FAIL
退出码：0=PASS，1=FAIL，2=读不了
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

AUDIO_EXT = {".wav", ".flac", ".mp3", ".aiff", ".aif", ".m4a", ".ogg"}
MIN_SECONDS, MAX_SECONDS = 90.0, 240.0
MIN_SR = 32000
MAX_CH = 2
MIN_RMS_DB = -60.0


def estimate_bpm(data, sr: int) -> float | None:
    """粗略 BPM 估算（给评委提示用）：谱通量 + 梳状自相关，偏好 60–200。"""
    try:
        import numpy as np
    except ImportError:  # pragma: no cover
        return None
    mono = data.mean(axis=1) if getattr(data, "ndim", 1) > 1 else data
    # 限 3 分钟、降混，控制开销
    mono = mono[: sr * 180].astype("float32")
    if len(mono) < sr * 2:
        return None
    hop, win = 512, 2048
    frames = 1 + (len(mono) - win) // hop
    if frames < 8:
        return None
    spec = np.abs(np.fft.rfft(
        np.lib.stride_tricks.as_strided(
            mono, shape=(frames, win),
            strides=(mono.strides[0] * hop, mono.strides[0])) * np.hanning(win), axis=1))
    flux = np.maximum(0.0, np.diff(np.log1p(spec), axis=0)).sum(axis=1)
    if flux.std() == 0:
        return None
    flux = (flux - flux.mean()) / flux.std()
    fps = sr / hop
    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    ac /= max(ac[0], 1e-9)

    def score(bpm: float) -> float:
        lag = fps * 60.0 / bpm
        if lag < 1 or lag >= len(ac) - 1:
            return 0.0
        i = int(round(lag))
        v = max(ac[i - 1], ac[i], ac[min(i + 1, len(ac) - 1)])
        # 加一点倍频/半频证据，减少倍速误判
        for mult in (2.0, 0.5):
            j = int(round(lag * mult))
            if 1 <= j < len(ac) - 1:
                v += 0.35 * max(ac[j - 1], ac[j], ac[j + 1])
        return float(v)

    best_bpm, best = None, -1e9
    for bpm in np.arange(60.0, 200.5, 0.25):
        sc = score(bpm)
        if sc > best:
            best, best_bpm = sc, float(bpm)
    return round(best_bpm, 1) if best_bpm else None


def check(path: Path, expect_bpm: float | None = None,
          bpm_tol: float | None = None) -> dict:
    r = {"entry": str(path), "pass": False, "fail": [], "warn": [], "info": {}}
    if path.suffix.lower() not in AUDIO_EXT:
        r["fail"].append(f"不是音频扩展名：{path.suffix}（允许 {sorted(AUDIO_EXT)}）")
        return r
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as exc:  # pragma: no cover
        r["fail"].append(f"缺少依赖 numpy/soundfile：{exc}")
        return r
    try:
        info = sf.info(str(path))
        data, sr = sf.read(str(path), always_2d=True, dtype="float32")
    except Exception as exc:
        r["fail"].append(f"无法解码：{type(exc).__name__}: {exc}")
        return r

    dur = len(data) / sr
    rms = float(np.sqrt(np.mean(data ** 2))) if data.size else 0.0
    rms_db = 20 * np.log10(max(rms, 1e-12))
    peak = float(np.max(np.abs(data))) if data.size else 0.0
    peak_db = 20 * np.log10(max(peak, 1e-12))
    bpm_est = estimate_bpm(data, sr)
    r["info"] = {
        "sample_rate": sr, "channels": data.shape[1], "bpm_estimate": bpm_est,
        "duration_sec": round(dur, 2), "peak_dbfs": round(peak_db, 2),
        "rms_dbfs": round(rms_db, 2), "format": info.format, "subtype": info.subtype,
    }
    if dur < MIN_SECONDS:
        r["fail"].append(f"时长 {dur:.1f}s < {MIN_SECONDS:.0f}s")
    elif dur > MAX_SECONDS:
        r["fail"].append(f"时长 {dur:.1f}s > {MAX_SECONDS:.0f}s")
    if rms_db < MIN_RMS_DB:
        r["fail"].append(f"几乎静音（RMS {rms_db:.1f} dBFS < {MIN_RMS_DB}）")
    if sr < MIN_SR:
        r["warn"].append(f"采样率 {sr} < {MIN_SR}")
    if data.shape[1] > MAX_CH:
        r["warn"].append(f"声道数 {data.shape[1]} > {MAX_CH}")
    if expect_bpm is not None:
        if bpm_est is None:
            r["warn"].append(f"BPM 估算失败，需人工确认是否 {expect_bpm:.0f} BPM")
        else:
            candidates = [bpm_est, bpm_est / 2, bpm_est * 2]
            tol = 2.0 if bpm_tol is None else bpm_tol
            hit = min(candidates, key=lambda x: abs(x - expect_bpm))
            delta = abs(hit - expect_bpm)
            ratio_note = "" if hit == bpm_est else "（与估算值为半速/倍速关系）"
            note = (f"BPM 估算 {bpm_est:g}，候选 {hit:g}{ratio_note}"
                    f"；主题要求 {expect_bpm:.0f}" + (f" ±{tol:g}" if tol else ""))
            if delta <= tol and hit == bpm_est:
                r["info"]["bpm_check"] = f"OK：{note}"
            elif delta <= tol:
                # 只有半速/倍速才勉强对上 → 不直接判 OK，交人工确认
                r["warn"].append(f"需人工确认（倍速/半速歧义）：{note}")
            elif delta <= tol + 8:
                r["warn"].append(f"接近边界，需人工确认：{note}（差 {delta:.1f}）")
            else:
                r["fail"].append(f"BPM 明显不符：{note}（差 {delta:.1f}）")
    r["pass"] = not r["fail"]
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="校验音乐 AI 组音频作品")
    ap.add_argument("audio", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--expect-bpm", type=float, default=None,
                    help="主题要求的 BPM（theme-a=90 / theme-b=180）")
    ap.add_argument("--bpm-tolerance", type=float, default=None,
                    help="BPM 容差（theme-a 用 2；theme-b 用 20）")
    a = ap.parse_args(argv)
    results = [check(Path(p), a.expect_bpm, a.bpm_tolerance) for p in a.audio]
    if a.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            i = r["info"]
            print(f"\n=== {r['entry']} ===")
            if i:
                print(f"  {i['format']}/{i['subtype']} | {i['sample_rate']} Hz | "
                      f"{i['channels']} ch | {i['duration_sec']}s | "
                      f"peak {i['peak_dbfs']} dBFS | RMS {i['rms_dbfs']} dBFS")
                if i.get("bpm_estimate"):
                    print(f"  BPM 估算 ≈ {i['bpm_estimate']:g}（仅供人工参考）")
                if i.get("bpm_check"):
                    print(f"  ✓ {i['bpm_check']}")
            for f in r["fail"]:
                print(f"  ❌ {f}")
            for w in r["warn"]:
                print(f"  ⚠️  {w}")
            print(f"  → {'PASS' if r['pass'] else 'FAIL'}")
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
