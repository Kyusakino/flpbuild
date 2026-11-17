#!/usr/bin/env python3
"""flpbuild.py — 把 JSON 编排规格写成可打开的 .flp（供比赛参赛 AI 使用）

它只做「格式转换」：你决定音乐（音高、时值、节奏、力度、编排），它负责把
这些决定写进 FL Studio 工程。**不包含任何作曲逻辑**。

用法：
    python3 tools/flpbuild.py --spec 我的编排.json --out submissions/theme-b/text-ai/<id>/track.flp
    python3 tools/flpbuild.py --spec 我的编排.json --out .../track.flp --midi .../track.mid

规格（JSON）：
{
  "bpm": 180,
  "channel_names": ["Piano", "Strings"],         // 可选，最多 11 个，按通道顺序
  "patterns": [
    {
      "name": "Intro-Strings",
      "channel": 3,                              // 0..(模板通道数-1)
      "notes": [                                 // [小节, 拍, 时值(拍), MIDI 音高, 力度]
        [1, 1, 2, 60, 100],
        [1, 3, 2, 64, 96]
      ]
    }
  ],
  "arrangement": [                               // playlist：pattern 放在第几小节、占几小节
    {"pattern": 0, "bar": 1,  "bars": 8},
    {"pattern": 1, "bar": 9,  "bars": 8}
  ]
}

坐标约定：
  * 小节、拍都从 1 开始（4/4；1 小节 = 4 拍 = 4 × PPQ ticks）
  * 时值单位是「拍」（1 = 四分音符，0.5 = 八分音符）
  * 音高用 MIDI 编号（60 = C4，69 = A4）；力度 1..127
  * 输出时长 = arrangement 覆盖的总小节数；需在 90–240 秒之间（校验器会查）

成功时打印统计（pattern/音符数/BPM/时长/大小）；失败返回非 0。
写完会重新读回验证，并可选跑 check_entry。
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from flpkit import FLP, Note, Event, encode_text  # noqa: E402

DEFAULT_TEMPLATE = ROOT / "templates" / "blank-11ch.flp"
MAX_CHANNELS = 16   # 默认模板 11；用 --template 时以模板实际通道数为准
MAX_PATTERNS = 16
EV_CHANNEL_NEW = 64
EV_CHANNEL_NAME = 192


def err(msg: str) -> None:
    print(f"✗ {msg}", file=sys.stderr)


def load_spec(path: Path) -> dict:
    try:
        spec = json.loads(path.read_text())
    except Exception as exc:
        raise SystemExit(f"✗ 规格不是合法 JSON：{exc}")
    if not isinstance(spec, dict):
        raise SystemExit("✗ 规格顶层必须是对象")
    if "bpm" not in spec:
        raise SystemExit("✗ 缺少 bpm")
    bpm = spec["bpm"]
    if not isinstance(bpm, (int, float)) or not (20 <= float(bpm) <= 400):
        raise SystemExit("✗ bpm 必须是 20..400 的数字")
    if not spec.get("patterns"):
        raise SystemExit("✗ patterns 不能为空")
    if len(spec["patterns"]) > MAX_PATTERNS:
        raise SystemExit(f"✗ patterns 最多 {MAX_PATTERNS} 个（模板 16 个）")
    return spec


def build(spec: dict, out: Path, template: Path, midi_out: Path | None) -> int:
    flp = FLP.load(template)
    ppq = flp.ppq
    bpm = float(spec["bpm"])
    bar_ticks = ppq * 4

    patterns = spec["patterns"]
    if len(flp.patterns()) < len(patterns):
        raise SystemExit(f"✗ 模板只有 {len(flp.patterns())} 个 pattern，规格给了 {len(patterns)} 个")

    # 通道名（插到每个 ChannelID.New 之后；模板默认没有名字事件）
    names = spec.get("channel_names") or []
    if len(names) > flp.channels:
        raise SystemExit(f"✗ channel_names 最多 {flp.channels} 个（模板通道数）")
    id_positions = [i for i, ev in enumerate(flp.events) if ev.id == EV_CHANNEL_NEW]
    for offset, (pos, name) in enumerate(zip(id_positions, names)):
        flp.events.insert(pos + 1 + offset, Event(EV_CHANNEL_NAME, encode_text(str(name))))

    # 先清空全部 pattern（模板里是空的，但允许复用带内容的模板）
    for i in range(len(flp.patterns())):
        flp.save_notes(i, [])
        flp.set_pattern_name(i, f"Pattern {i+1:02d}")

    total_notes = 0
    for pi, pat in enumerate(patterns):
        ch = pat.get("channel", pi)
        if not isinstance(ch, int) or not (0 <= ch < flp.channels):
            raise SystemExit(f"✗ patterns[{pi}].channel 必须是 0..{flp.channels-1}（模板通道数）")
        notes = []
        for ni, n in enumerate(pat.get("notes", [])):
            if not (isinstance(n, list) and len(n) == 5):
                raise SystemExit(f"✗ patterns[{pi}].notes[{ni}] 必须是 "
                                 f"[小节, 拍, 时值拍, 音高, 力度]")
            bar, beat, dur, key, vel = n
            if not (1 <= bar and 1 <= beat < 5):
                raise SystemExit(f"✗ patterns[{pi}].notes[{ni}] 小节/拍不合法（拍 1 起，可含小数，<5）：{n}")
            if not (0 < dur and 0 < key <= 131 and 1 <= vel <= 127):
                raise SystemExit(f"✗ patterns[{pi}].notes[{ni}] 时值/音高/力度不合法：{n}")
            pos = int(round(((bar - 1) * 4 + (beat - 1)) * ppq))
            length = max(1, int(round(dur * ppq)))
            notes.append(Note(position=pos, length=length, key=int(key),
                              velocity=int(vel), rack_channel=int(ch)))
        flp.save_notes(pi, notes)
        flp.set_pattern_name(pi, str(pat.get("name") or f"Pattern {pi+1:02d}"))
        total_notes += len(notes)

    # 编排
    arrangement = spec.get("arrangement") or []
    if not arrangement:
        raise SystemExit("✗ arrangement 不能为空（至少放一个 pattern 到第 1 小节）")
    clips = []
    end_bars = 0
    for ci, c in enumerate(arrangement):
        pi, bar, bars = c.get("pattern"), c.get("bar"), c.get("bars")
        if type(pi) is not int or not (0 <= pi < len(patterns)):
            raise SystemExit(f"✗ arrangement[{ci}].pattern 越界：{pi}")
        if type(bar) is not int or bar < 1:
            raise SystemExit(f"✗ arrangement[{ci}].bar 必须是 ≥1 的整数")
        if type(bars) is not int or bars < 1:
            raise SystemExit(f"✗ arrangement[{ci}].bars 必须是 ≥1 的整数")
        track = c.get("track", patterns[pi].get("channel", pi) + 1)
        if type(track) is not int or not (1 <= track <= 500):
            raise SystemExit(f"✗ arrangement[{ci}].track 必须是 1..500")
        clips.append(dict(position=(bar - 1) * bar_ticks, pattern=pi,
                          length=bars * bar_ticks, track=track))
        end_bars = max(end_bars, bar - 1 + bars)
    flp.set_playlist_clips(clips)
    flp.set_tempo(bpm)

    seconds = end_bars * 4 * 60.0 / bpm
    if seconds < 90 or seconds > 240:
        print(f"⚠ 时长 {seconds:.1f}s 不在 90–240s：check_entry 会 FAIL；"
              f"调整 arrangement 总小节数（当前 {end_bars}）")

    out.parent.mkdir(parents=True, exist_ok=True)
    size = flp.save(out)

    # 逐字段读回，并以独立原生字段校验器复核（不能只比较片段数量）。
    chk = FLP.load(out)
    got_notes = sum(len(p.notes) for p in chk.patterns())
    got_clips = chk.playlist_clips()
    from check_entry import parse_flp, validate_playlist
    native_clips, native_errors = validate_playlist(parse_flp(out)['events'])
    expected = [(c['position'], c['pattern'], c['length'], c['track'], 0, c['length']) for c in clips]
    actual = [(c['position_ticks'], c['pattern'], c['length_ticks'], c['track'],
               c['start_offset_ticks'], c['end_offset_ticks']) for c in got_clips]
    expected_ids = [chk.patterns()[c['pattern']].iid for c in clips]
    ok = (got_notes == total_notes and abs((chk.tempo or 0) - bpm) < 0.01
          and actual == expected and not native_errors
          and [c['pattern_id'] for c in native_clips] == expected_ids)
    print(f"输出 {out}")
    print(f"  {len(patterns)} patterns / {total_notes} 音符 / {len(clips)} clips / "
          f"{end_bars} 小节 ≈ {seconds:.1f}s @ {bpm:g} BPM")
    print(f"  {size} bytes；读回校验：{'ok' if ok else 'MISMATCH'}")
    if not ok:
        for error in native_errors:
            print(f'  ✗ {error}')
        if actual != expected:
            print('  ✗ Playlist 位置、Pattern、长度、轨道或来源偏移与规格不一致')
        return 1

    if midi_out:
        write_midi(midi_out, bpm, ppq, patterns, clips)
        print(f"  MIDI -> {midi_out}")

    if spec.get("_check"):
        cmd = [sys.executable, str(ROOT / "tools" / "check_entry.py"), str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        print(r.stdout.strip())
        if r.returncode != 0:
            return r.returncode
    return 0


# ---------------- 简易 MIDI 导出（给「备份」用）-----------------------------
def _vlq(value: int) -> bytes:
    buf = bytearray([value & 0x7F])
    value >>= 7
    while value:
        buf.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(buf)


def write_midi(path: Path, bpm: float, ppq: int, patterns: list[dict], clips: list[dict]) -> None:
    events: list[tuple[int, int, bytes]] = []
    for clip in clips:
        pi = clip["pattern"]
        track_ch = int(patterns[pi].get("channel", pi)) & 0x0F
        base = clip["position"]
        limit = clip["position"] + clip["length"]
        for n in patterns[pi].get("notes", []):
            bar, beat, dur, key, vel = n
            pos = int(round(((bar - 1) * 4 + (beat - 1)) * ppq))
            length = max(1, int(round(dur * ppq)))
            if base + pos >= limit:
                continue
            events.append((base + pos, 0, bytes([0x90 | track_ch, int(key), int(vel)])))
            events.append((base + pos + length, 0,
                           bytes([0x80 | track_ch, int(key), 0])))
    events.sort(key=lambda e: (e[0], e[1]))

    def track_chunk(evs: list[tuple[int, bytes]]) -> bytes:
        data = bytearray()
        last = 0
        for tick, msg in evs:
            data += _vlq(tick - last) + msg
            last = tick
        data += _vlq(0) + b"\xFF\x2F\x00"
        return b"MTrk" + struct.pack(">I", len(data)) + bytes(data)

    micros = int(round(60_000_000 / bpm))
    tempo = (b"\x00\xFF\x51\x03" + micros.to_bytes(3, "big") +
             b"\x00\xFF\x2F\x00")
    tracks = [b"MTrk" + struct.pack(">I", len(tempo)) + tempo]
    tracks.append(track_chunk([(t, m) for t, _o, m in events]))
    out = b"MThd" + struct.pack(">IHHH", 6, 1, len(tracks), ppq) + b"".join(tracks)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", required=True, help="编排规格 JSON")
    ap.add_argument("--out", required=True, help="输出 .flp 路径")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE),
                    help="起步工程（默认 templates/blank-11ch.flp）")
    ap.add_argument("--midi", default=None, help="同时导出 track.mid（可选）")
    ap.add_argument("--check", action="store_true", help="写完后跑一次 check_entry.py")
    a = ap.parse_args(argv)
    spec = load_spec(Path(a.spec))
    if a.check:
        spec["_check"] = True
    return build(spec, Path(a.out), Path(a.template), Path(a.midi) if a.midi else None)


if __name__ == "__main__":
    sys.exit(main())
