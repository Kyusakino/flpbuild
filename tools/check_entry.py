#!/usr/bin/env python3
"""check_entry.py — 参赛作品 (.flp) 自动校验

只做「硬门槛」判定，不评价音乐好坏：
  * 文件结构是否是 FL Studio 工程（FLhd/FLdt）
  * FL 版本、PPQ、通道数、pattern 数
  * 音符总数、时值、音高范围
  * Playlist 原生记录、Pattern 引用、片段偏移及编排时长（按初始 BPM）
  * 体积上限

用法：
    python3 tools/check_entry.py submissions/<ID>/track.flp
    python3 tools/check_entry.py submissions/*/track.flp --json > report.json
退出码：0 = PASS，1 = FAIL，2 = 文件读不了
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

WORD, DWORD, TEXT = 64, 128, 192
NOTE_SIZE = 24
CLIP_SIZE = 88
VALID_PPQ = (24, 48, 72, 96, 120, 144, 168, 192, 384, 768, 960)

# 阈值（在 RULES.md 中公开，可调）
MIN_NOTES = 64
MAX_BYTES = 50 * 1024 * 1024
MIN_SECONDS = 90.0
MAX_SECONDS = 240.0
MAX_CHANNELS = 16
MIN_KEY, MAX_KEY = 0, 131


def read_varint(buf: bytes, i: int):
    v = shift = 0
    while True:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << shift
        if not b & 0x80:
            return v, i
        shift += 7


def parse_flp(path: Path):
    raw = path.read_bytes()
    if raw[:4] != b"FLhd":
        raise ValueError("不是 FLP 文件（缺 FLhd 头）")
    hdr = struct.unpack("<I", raw[4:8])[0]
    fmt, channels, ppq = struct.unpack("<HHH", raw[8:8 + hdr])
    dt = 8 + hdr
    if raw[dt:dt + 4] != b"FLdt":
        raise ValueError("缺 FLdt 数据块")
    dt = 8 + hdr
    body_start = dt + 8
    body_end = body_start + struct.unpack("<I", raw[dt + 4:dt + 8])[0]

    def parse_from(start):
        events, i = [], start
        while i < body_end:
            eid = raw[i]
            i += 1
            if eid < WORD:
                payload, i = raw[i:i + 1], i + 1
            elif eid < DWORD:
                payload, i = raw[i:i + 2], i + 2
            elif eid < TEXT:
                payload, i = raw[i:i + 4], i + 4
            else:
                n, i = read_varint(raw, i)
                payload, i = raw[i:i + n], i + n
            events.append((eid, payload))
        if i != body_end:
            raise ValueError("事件流长度与数据块不符")
        return events

    # FL 2026 在事件流前有一段「项目信息块」（本机实测 152 字节）。真正的流起点
    # 以事件 237（项目时间戳，2 个 float64）为标志；找不到就退回 FL<=25 的 22。
    stream_start, events = body_start, []
    try:
        events = parse_from(body_start)
    except (ValueError, IndexError, struct.error):
        pass
    for candidate in range(body_start, min(body_start + 400, body_end - 24)):
        if raw[candidate] != 237:
            continue
        try:
            n, vi = read_varint(raw, candidate + 1)
            if n != 16:
                continue
        except Exception:
            continue
        try:
            ev = parse_from(candidate)
        except Exception:
            continue
        if any(e[0] == 65 for e in ev):   # 必须含 pattern 起始事件
            stream_start, events = candidate, ev
            break
    if not events:
        raise ValueError('无法定位有效的 FL 事件流')

    # 版本事件在信息块里（不属于事件流），单独从块首解析一次
    prefix_version = None
    if raw[body_start] == 199:
        try:
            vlen, vi = read_varint(raw, body_start + 1)
            prefix_version = raw[vi:vi + vlen].decode("ascii", "replace").rstrip("\x00")
        except Exception:
            pass

    return dict(fmt=fmt, channels=channels, ppq=ppq, events=events,
                size=len(raw), stream_start=stream_start,
                prefix_version=prefix_version)


def utf16(b: bytes) -> str:
    try:
        return b.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return b.decode("latin-1", "replace")


def validate_playlist(events) -> tuple[list[dict], list[str]]:
    """独立解码原始字段，不用 flpkit 的写入/读取结果互相证明正确。"""
    pattern_ids = {int.from_bytes(p, 'little') for eid, p in events if eid == 65}
    channel_ids = {int.from_bytes(p, 'little') for eid, p in events if eid == 64}
    clips, errors = [], []
    for eid, payload in events:
        if eid != 233:
            continue
        if len(payload) % CLIP_SIZE:
            errors.append(f'Playlist 数据 {len(payload)} 字节，非支持的 88 字节记录')
            continue
        for offset in range(0, len(payload), CLIP_SIZE):
            label = f'Playlist 片段 {len(clips) + 1}'
            pos, base, item, length, track = struct.unpack_from('<IHHIH', payload, offset)
            marker, flags = struct.unpack_from('<HH', payload, offset + 16)
            if base != 20480:
                errors.append(f'{label} Pattern 基准值 {base}，应为 20480')
            if not length or pos + length > 0xFFFFFFFF:
                errors.append(f'{label} 位置/长度非法')
            if track >= 500:
                errors.append(f'{label} 轨道越界')
            if marker != 120 or not flags & 64:
                errors.append(f'{label} 缺少有效原生标记')
            iid = None
            start = end = None
            if item > base:
                iid = item - base
                if iid not in pattern_ids:
                    errors.append(f'{label} 引用了不存在的 Pattern ID {iid}')
                start, end = struct.unpack_from('<II', payload, offset + 24)
                start = None if start in (0xFFFFFFFF, 0xBF800000) else start
                end = None if end in (0xFFFFFFFF, 0xBF800000) else end
                if start is not None and end is not None and end <= start:
                    errors.append(f'{label} Pattern 来源终点不大于起点')
            elif item not in channel_ids:
                errors.append(f'{label} 引用了不存在的通道 ID {item}（检查零基 Pattern 编号）')
            clips.append({'position_ticks': pos, 'length_ticks': length,
                          'track': 500 - track, 'pattern_id': iid,
                          'start_offset_ticks': start, 'end_offset_ticks': end})
    return clips, errors


def analyse(info) -> dict:
    ev = info["events"]
    out = {
        "size_bytes": info["size"],
        "format": info["fmt"],
        "channels": info["channels"],
        "ppq": info["ppq"],
        "version": None,
        "tempo_bpm": None,
        "patterns": 0,
        "notes": 0,
        "note_min": None,
        "note_max": None,
        "bad_length": 0,
        "song_bars": 0.0,
        "note_bars": 0.0,
        "playlist_clips": 0,
        "playlist_errors": [],
        "seconds": None,
        "channel_names": [],
    }
    if info.get("prefix_version"):
        out["version"] = info["prefix_version"]
    for eid, p in ev:
        if eid == 199 and len(p) < 40 and not out["version"]:  # FLVersion
            out["version"] = p.decode("ascii", "replace").rstrip("\x00")
        elif eid == 156 and len(p) == 4:
            bpm = struct.unpack("<I", p)[0] / 1000.0
            if out["tempo_bpm"] is None and 20.0 <= bpm <= 400.0:
                out["tempo_bpm"] = bpm
        elif eid == 65:
            out["patterns"] += 1
        elif eid == 192 and len(p) % 2 == 0:
            out["channel_names"].append(utf16(p))
        elif eid == 224 and len(p) % NOTE_SIZE == 0:
            for k in range(0, len(p), NOTE_SIZE):
                pos, _flags, _ch, length, key, *_ = struct.unpack_from("<IHHIHHBBBBBBBB", p, k)
                out["notes"] += 1
                out["note_min"] = key if out["note_min"] is None else min(out["note_min"], key)
                out["note_max"] = key if out["note_max"] is None else max(out["note_max"], key)
                if length <= 0:
                    out["bad_length"] += 1
                if info['ppq'] > 0:
                    out["note_bars"] = max(out["note_bars"], (pos + length) / (info["ppq"] * 4))
    out["patterns"] = len({int.from_bytes(p, 'little') for eid, p in ev if eid == 65})
    clips, out['playlist_errors'] = validate_playlist(ev)
    out['playlist_clips'] = len(clips)
    if clips and info['ppq'] > 0:
        out['song_bars'] = max(c['position_ticks'] + c['length_ticks'] for c in clips) / (info['ppq'] * 4)
    bpm = out["tempo_bpm"] or 120.0
    out["seconds"] = round(out["song_bars"] * 4 * 60.0 / bpm, 2)
    return out


def check(path: Path, expect_bpm: float | None = None, bpm_tol: float = 2.0) -> dict:
    r = {"entry": str(path), "pass": False, "fail": [], "warn": []}
    try:
        info = parse_flp(path)
    except Exception as exc:
        r["fail"].append(f"无法解析：{type(exc).__name__}: {exc}")
        return r
    a = analyse(info)
    r["info"] = a
    r['fail'].extend(a['playlist_errors'])
    if a['playlist_clips'] == 0:
        r['fail'].append('Playlist 没有编排片段，不能以 Pattern 内的音符长度替代整曲时长')

    if not a["version"]:
        r["fail"].append("缺少 FL 版本信息（可能不是 FL 保存的工程）")
    elif a["version"].split(".")[0].isdigit() and int(a["version"].split(".")[0]) < 20:
        r["warn"].append(f"FL 版本较老：{a['version']}")
    if a["ppq"] not in VALID_PPQ:
        r["fail"].append(f"PPQ={a['ppq']} 非法（允许 {VALID_PPQ}）")
    if a["channels"] <= 0:
        r["fail"].append("工程没有通道")
    elif a["channels"] > MAX_CHANNELS:
        r["warn"].append(f"通道数 {a['channels']} > {MAX_CHANNELS}")
    if a["size_bytes"] > MAX_BYTES:
        r["fail"].append(f"文件 {a['size_bytes']/1e6:.1f} MB 超过 {MAX_BYTES/1e6:.0f} MB")
    if a["notes"] < MIN_NOTES:
        r["fail"].append(f"音符只有 {a['notes']} 个（至少 {MIN_NOTES}）")
    if a["bad_length"]:
        r["fail"].append(f"{a['bad_length']} 个音符时值 <= 0")
    if a["note_min"] is not None and (a["note_min"] < MIN_KEY or a["note_max"] > MAX_KEY):
        r["fail"].append(f"音高超出 0..131：{a['note_min']}..{a['note_max']}")
    if a["seconds"] is not None:
        if a["seconds"] < MIN_SECONDS:
            r["fail"].append(f"时长约 {a['seconds']}s，短于 {MIN_SECONDS}s")
        elif a["seconds"] > MAX_SECONDS:
            r["fail"].append(f"时长约 {a['seconds']}s，长于 {MAX_SECONDS}s")
    if a["patterns"] <= 0:
        r["warn"].append("没有 pattern（可能没有音符容器）")

    if expect_bpm is not None and a["tempo_bpm"] is not None:
        if abs(a["tempo_bpm"] - expect_bpm) > bpm_tol:
            r["fail"].append(f"BPM {a['tempo_bpm']:.1f} 超出主题要求 "
                             f"{expect_bpm:.0f} ±{bpm_tol:g}（允许 "
                             f"{expect_bpm - bpm_tol:.0f}–{expect_bpm + bpm_tol:.0f}）")
    elif expect_bpm is not None:
        r["warn"].append(f"工程没有可读的初始 BPM，无法核对 {expect_bpm:.0f}")

    r["pass"] = not r["fail"]
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description="校验 FLP 参赛作品硬门槛")
    ap.add_argument("flp", nargs="+")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--expect-bpm", type=float, default=None,
                    help="主题要求的初始 BPM（theme-a=90 / theme-b=180）")
    ap.add_argument("--bpm-tolerance", type=float, default=2.0,
                    help="BPM 容差（theme-a 用 2；theme-b 用 20）")
    args = ap.parse_args(argv)
    results = [check(Path(p), args.expect_bpm, args.bpm_tolerance) for p in args.flp]
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            a = r.get("info", {})
            print(f"\n=== {r['entry']} ===")
            if "info" in r:
                print(f"  FL 版本 {a['version']} | PPQ {a['ppq']} | 通道 {a['channels']} | "
                      f"pattern {a['patterns']} | 音符 {a['notes']} | "
                      f"{a['song_bars']:.1f} 小节 ≈ {a['seconds']}s | BPM {a['tempo_bpm']}")
            for f in r["fail"]:
                print(f"  ❌ {f}")
            for w in r["warn"]:
                print(f"  ⚠️  {w}")
            print(f"  → {'PASS' if r['pass'] else 'FAIL'}")
    return 0 if all(r["pass"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
