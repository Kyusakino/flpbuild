#!/usr/bin/env python3
"""flpkit — 最小 FLP 读写库（只提供原语，不提供作曲逻辑）

你能做的：
  * 读：工程头（PPQ/通道数）、事件流、pattern 与音符、通道名、BPM、playlist clips
  * 改：音符、通道名、BPM、事件 payload；也可以插入/删除自己的事件
  * 存：写回 .flp（自动修正 FLdt 长度）

你需要自己决定的：写什么音乐、怎么组织 pattern/clip、用什么音源。
本库不包含任何"生成曲子"的代码。

快速开始
--------
    from flpkit import FLP, Note

    proj = FLP.load("templates/blank-16ch.flp")
    proj.save_notes(0, [Note(position=0, length=96, key=60, velocity=100)])
    proj.set_playlist_clips([dict(position=0, pattern=0, length=384, track=1)])
    proj.set_tempo(120)
    proj.save("submissions/me/theme-a/track.flp")

格式备忘（FL Studio 2026，本机实测）
------------------------------------
  FLhd | u32 hdr_len(=6) | u16 format, u16 channels, u16 ppq
  FLdt | u32 body_len   | 事件流
  事件: id(u8) + payload
    id <64   1 字节 | <128  2 字节 | <192 4 字节 | >=192  LEB128 长度 + 数据
  FL 26 的事件流前有一段"项目信息块"，本库用事件 237（时间戳）定位真正起点。
  音符（事件 224）：每条 24 字节
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

WORD, DWORD, TEXT = 64, 128, 192

FLHD = b"FLhd"
FLDT = b"FLdt"

EV_CHANNEL_NEW = 64       # ChannelID.New
EV_CHANNEL_NAME = 192     # ChannelID.Name (UTF-16LE)
EV_PATTERN_NEW = 65       # PatternID.New（每个 pattern 写两次）
EV_PATTERN_NAME = 193     # PatternID.Name (UTF-16LE)
EV_PATTERN_NOTES = 224    # PatternID.Notes
EV_PATTERN_CTRL = 223     # PatternID.Controllers
EV_PROJECT_TEMPO = 156    # ProjectID.Tempo（BPM × 1000）
EV_PROJECT_TIMESTAMP = 237
EV_PLAYLIST_CLIPS = 233   # 本工具写入 FL 26.1.7 的 88 字节/条格式

NOTE = struct.Struct("<IHHIHHBBBBBBBB")  # 24 bytes
CLIP = struct.Struct("<IHHIH")           # 前 14 字节（共 88）
CLIP_SIZE = 88
PATTERN_BASE = 0x5000
PLAYLIST_VERSION = '26.1.7.5421'
OFFSET_AUTO = (0xFFFFFFFF, 0xBF800000)


def _read_varint(buf: bytes, i: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def _write_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        g = value & 0x7F
        value >>= 7
        out.append(g | 0x80 if value else g)
        if not value:
            return bytes(out)


@dataclass
class Event:
    id: int
    payload: bytes

    def payload_u32(self) -> int:
        return struct.unpack("<I", self.payload)[0]

    def text(self) -> str:
        return decode_text(self.payload)


@dataclass
class Note:
    position: int      # ticks
    length: int        # ticks
    key: int           # MIDI 0..131
    velocity: int      # 0..127
    rack_channel: int = 0
    flags: int = 0x4000
    group: int = 0
    fine_pitch: int = 120
    release: int = 0
    midi_channel: int = 0
    pan: int = 64
    mod_x: int = 128
    mod_y: int = 128

    @classmethod
    def from_bytes(cls, b: bytes) -> "Note":
        (pos, flags, rack, length, key, group, fine, _u1,
         rel, midi, pan, vel, mx, my) = NOTE.unpack(b)
        return cls(pos, length, key, vel, rack, flags, group, fine, rel, midi, pan, mx, my)

    def to_bytes(self) -> bytes:
        return NOTE.pack(self.position, self.flags, self.rack_channel, self.length,
                         self.key, self.group, self.fine_pitch, 0, self.release,
                         self.midi_channel, self.pan, self.velocity, self.mod_x, self.mod_y)


def decode_text(b: bytes) -> str:
    try:
        return b.decode("utf-16-le").rstrip("\x00")
    except UnicodeDecodeError:
        return b.decode("latin-1", "replace")


def encode_text(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


@dataclass
class Pattern:
    name: str = ""
    notes: list[Note] = field(default_factory=list)
    rack_channel: int | None = None
    _notes_ev: int | None = None
    _name_ev: int | None = None
    iid: int = 0                    # 原生 Pattern ID；与列表的零基下标不同


class FLP:
    """FL Studio 工程。`events` 是原始事件列表，可直接读写。"""

    def __init__(self, fmt: int, channels: int, ppq: int, events: list[Event],
                 prefix: bytes = b"", stream_start: int = 22):
        self.format = fmt
        self.channels = channels
        self.ppq = ppq
        self.events = events
        self.prefix = prefix
        self.stream_start = stream_start

    # ---------------- 读 ----------------
    @classmethod
    def load(cls, path: str | Path) -> "FLP":
        raw = Path(path).read_bytes()
        if raw[:4] != FLHD:
            raise ValueError("不是 FLP：缺 FLhd")
        hdr_len = struct.unpack("<I", raw[4:8])[0]
        fmt, channels, ppq = struct.unpack("<HHH", raw[8:8 + hdr_len])
        dt = 8 + hdr_len
        if raw[dt:dt + 4] != FLDT:
            raise ValueError("不是 FLP：缺 FLdt")
        body_start = dt + 8
        body_end = body_start + struct.unpack("<I", raw[dt + 4:dt + 8])[0]

        def parse_from(start: int) -> list[Event]:
            ev, i = [], start
            while i < body_end:
                eid = raw[i]
                i += 1
                if eid < WORD:
                    p, i = raw[i:i + 1], i + 1
                elif eid < DWORD:
                    p, i = raw[i:i + 2], i + 2
                elif eid < TEXT:
                    p, i = raw[i:i + 4], i + 4
                else:
                    n, i = _read_varint(raw, i)
                    p, i = raw[i:i + n], i + n
                ev.append(Event(eid, p))
            if i != body_end:
                raise ValueError("事件流越界")
            return ev

        try:
            start, events = body_start, parse_from(body_start)
        except ValueError:
            start, events = None, []
        for cand in range(body_start, min(body_start + 400, body_end - 24)):
            if raw[cand] != EV_PROJECT_TIMESTAMP:
                continue
            try:
                n, vi = _read_varint(raw, cand + 1)
                if n != 16:
                    continue
                events_try = parse_from(cand)
            except Exception:
                continue
            if any(e.id == EV_PATTERN_NEW for e in events_try):
                start, events = cand, events_try
                break
        if start is None or not events:
            raise ValueError("无法定位事件流起点（文件可能不是 FL 工程）")
        # 版本事件在"项目信息块"里（不在事件流中）
        prefix_version = None
        if raw[body_start] == 199:
            try:
                n, vi = _read_varint(raw, body_start + 1)
                prefix_version = raw[vi:vi + n].decode("ascii", "replace").rstrip("\x00")
            except Exception:
                pass
        obj = cls(fmt, channels, ppq, events, raw[body_start:start], start)
        obj.prefix_version = prefix_version
        return obj

    # ---------------- 存 ----------------
    def to_bytes(self) -> bytes:
        events = bytearray()
        for ev in self.events:
            events.append(ev.id)
            if ev.id >= TEXT:
                events += _write_varint(len(ev.payload))
            events += ev.payload
        body = self.prefix + bytes(events)   # FLdt 长度包含项目信息块
        out = bytearray(FLHD) + struct.pack("<I", 6)
        out += struct.pack("<HHH", self.format, self.channels, self.ppq)
        out += FLDT + struct.pack("<I", len(body))
        return bytes(out) + body

    def save(self, path: str | Path) -> int:
        data = self.to_bytes()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(data)
        return len(data)

    # ---------------- 便捷读 ----------------
    @property
    def version(self) -> str | None:
        if getattr(self, "prefix_version", None):
            return self.prefix_version
        for ev in self.events:
            if ev.id == 199:
                return ev.payload.decode("ascii", "replace").rstrip("\x00")
        return None

    @property
    def tempo(self) -> float | None:
        for ev in self.events:
            if ev.id == EV_PROJECT_TEMPO and len(ev.payload) == 4:
                bpm = struct.unpack("<I", ev.payload)[0] / 1000.0
                if 20.0 <= bpm <= 400.0:
                    return bpm
        return None

    def set_tempo(self, bpm: float) -> None:
        payload = struct.pack("<I", int(round(bpm * 1000)))
        for ev in self.events:
            if ev.id == EV_PROJECT_TEMPO:
                ev.payload = payload
                return
        self.events.insert(1 if self.events and self.events[0].id == EV_PROJECT_TIMESTAMP else 0,
                           Event(EV_PROJECT_TEMPO, payload))

    @property
    def channel_names(self) -> list[str]:
        return [ev.text() for ev in self.events if ev.id == EV_CHANNEL_NAME]

    def set_channel_name(self, index: int, name: str) -> None:
        names = [i for i, ev in enumerate(self.events) if ev.id == EV_CHANNEL_NAME]
        if index < len(names):
            self.events[names[index]].payload = encode_text(name)

    def set_pattern_name(self, index: int, name: str) -> None:
        pats = self.patterns()
        if index < len(pats) and pats[index]._name_ev is not None:
            self.events[pats[index]._name_ev].payload = encode_text(name)

    def set_playlist_clips(self, clips: list[dict]) -> None:
        """写入第一个编排的 Pattern 片段，替换该编排原有片段。

        clips: [{position, pattern, length, track, start_offset?}]
        position/length/start_offset 用 ticks；pattern 是 patterns() 的零基
        下标，track 为 1..500。新片段使用完整原生默认记录，来源终点为
        start_offset + length，保留 Pattern 内有意的前后静音。
        只在输出对象中统一 FL 版本声明；不会修改输入模板文件。
        """
        targets = [ev for ev in self.events if ev.id == EV_PLAYLIST_CLIPS]
        if not targets:
            raise ValueError("工程里没有 playlist clips 事件（id 233）")
        patterns = self.patterns()
        ids = [p.iid for p in patterns]
        if len(ids) != len(set(ids)) or any(not 0 < iid <= 0xFFFF - PATTERN_BASE for iid in ids):
            raise ValueError('Pattern 原生 ID 重复或越界')

        def integer(value, label, minimum, maximum):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f'{label} 必须是 {minimum}..{maximum} 的整数')
            return value

        payload = bytearray()
        for i, c in enumerate(clips):
            pi = integer(c['pattern'], f'clips[{i}].pattern', 0, len(patterns) - 1)
            position = integer(c['position'], f'clips[{i}].position', 0, 0xFFFFFFFF)
            length = integer(c['length'], f'clips[{i}].length', 1, 0xFFFFFFFF - position)
            track = integer(c['track'], f'clips[{i}].track', 1, 500)
            offset = integer(c.get('start_offset', 0), f'clips[{i}].start_offset', 0, 0xFFFFFFFE - length)
            record = bytearray(CLIP_SIZE)
            CLIP.pack_into(record, 0, position, PATTERN_BASE, PATTERN_BASE + patterns[pi].iid,
                           length, 500 - track)
            struct.pack_into('<HHI', record, 16, 120, 64, 0x80806440)
            struct.pack_into('<II', record, 24, offset, offset + length)
            struct.pack_into('<I', record, 32, i + 1)
            struct.pack_into('<d', record, 64, 1.0)
            struct.pack_into('<I', record, 76, 0xFFFFFFFF)
            payload += record
        self._declare_playlist_version()
        targets[0].payload = bytes(payload)

    def _declare_playlist_version(self) -> None:
        """声明本写入器实际使用的原生记录版本，保留其余项目信息。"""
        version = PLAYLIST_VERSION.encode('ascii') + b'\0'
        if self.prefix:
            if self.prefix[0] != 199:
                raise ValueError('无法定位前置信息中的 FL 版本，停止写入 Playlist')
            size, start = _read_varint(self.prefix, 1)
            end = start + size
            tail = self.prefix[end:]
            if not tail or tail[0] != 159 or len(tail) < 5:
                raise ValueError('无法定位 FL build 信息，停止写入 Playlist')
            self.prefix = bytes([199]) + _write_varint(len(version)) + version
            self.prefix += bytes([159]) + struct.pack('<I', 5421) + tail[5:]
            self.stream_start = 22 + len(self.prefix)
        else:
            versions = [e for e in self.events if e.id == 199]
            builds = [e for e in self.events if e.id == 159]
            if not versions or not builds:
                raise ValueError('缺少 FL 版本/build 信息，停止写入 Playlist')
            versions[0].payload = version
            builds[0].payload = struct.pack('<I', 5421)
        self.prefix_version = PLAYLIST_VERSION

    def patterns(self) -> list[Pattern]:
        """按出现顺序配对「音符块」与「名称块」（FL 每个 pattern 写两次）。"""
        blocks: list[list[Event]] = []
        cur: list[Event] | None = None
        for ev in self.events:
            if ev.id == EV_PATTERN_NEW:
                if cur:
                    blocks.append(cur)
                cur = [ev]
            elif cur is not None:
                cur.append(ev)
        if cur:
            blocks.append(cur)
        note_blocks = [b for b in blocks if any(e.id == EV_PATTERN_NOTES for e in b)]
        name_blocks = [b for b in blocks if any(e.id == EV_PATTERN_NAME for e in b)]
        out = []
        for i, nb in enumerate(note_blocks):
            pat = Pattern()
            pat.iid = int.from_bytes(nb[0].payload, 'little')
            notes_ev = next(e for e in nb if e.id == EV_PATTERN_NOTES)
            if len(notes_ev.payload) % 24 == 0:
                pat.notes = [Note.from_bytes(notes_ev.payload[k:k + 24])
                             for k in range(0, len(notes_ev.payload), 24)]
            if i < len(name_blocks):
                name_ev = next(e for e in name_blocks[i] if e.id == EV_PATTERN_NAME)
                pat.name = name_ev.text()
                pat._name_ev = self.events.index(name_ev)
            pat._notes_ev = self.events.index(notes_ev)
            if pat.notes:
                pat.rack_channel = pat.notes[0].rack_channel
            out.append(pat)
        return out

    def save_notes(self, pattern_index: int, notes: list[Note]) -> None:
        pats = self.patterns()
        if not pats[pattern_index]._notes_ev:
            raise IndexError("pattern 不存在")
        payload = b"".join(n.to_bytes() for n in notes)
        self.events[pats[pattern_index]._notes_ev].payload = payload

    def playlist_clips(self) -> list[dict]:
        """读取原生 88 字节片段；拒绝旧写入器产生的错误记录。"""
        clips = []
        id_to_index = {p.iid: i for i, p in enumerate(self.patterns())}
        channel_ids = {int.from_bytes(e.payload, 'little') for e in self.events if e.id == EV_CHANNEL_NEW}
        for ev in self.events:
            if ev.id != EV_PLAYLIST_CLIPS:
                continue
            if len(ev.payload) % CLIP_SIZE:
                raise ValueError('Playlist 长度不符合本工具支持的 FL 26.1.7 / 88 字节片段格式')
            for k in range(0, len(ev.payload), CLIP_SIZE):
                raw = ev.payload[k:k + CLIP_SIZE]
                pos, base, item, length, trv = CLIP.unpack_from(raw)
                marker, flags = struct.unpack_from('<HH', raw, 16)
                if base != PATTERN_BASE:
                    raise ValueError(f'Playlist 片段基准值错误：{base}（应为 {PATTERN_BASE}）')
                if not length or pos + length > 0xFFFFFFFF or trv >= 500:
                    raise ValueError('Playlist 片段位置、长度或轨道非法')
                if marker != 120 or not flags & 64:
                    raise ValueError('Playlist 原生片段标记无效（不能使用全零记录）')
                if item > base:
                    iid = item - base
                    if iid not in id_to_index:
                        raise ValueError(f'Playlist 引用了不存在的 Pattern ID {iid}')
                    start, end = struct.unpack_from('<II', raw, 24)
                    start = None if start in OFFSET_AUTO else start
                    end = None if end in OFFSET_AUTO else end
                    if start is not None and end is not None and end <= start:
                        raise ValueError('Playlist Pattern 来源终点必须大于起点')
                    pattern = id_to_index[iid]
                    kind = 'pattern'
                else:
                    if item not in channel_ids:
                        raise ValueError(f'Playlist 引用了不存在的通道 ID {item}（可能把零基下标当作 Pattern ID）')
                    iid = pattern = None
                    kind = 'channel'
                    start, end = struct.unpack_from('<ff', raw, 24)
                clips.append(dict(position_ticks=pos, pattern=pattern, pattern_id=iid,
                                  kind=kind, channel_id=item if kind == 'channel' else None,
                                  length_ticks=length, track=500 - trv,
                                  start_offset_ticks=start, end_offset_ticks=end, raw=raw))
        return clips


__all__ = ["FLP", "Event", "Note", "Pattern", "decode_text", "encode_text"]
