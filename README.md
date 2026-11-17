# flpbuild

Minimal **FL Studio project (`.flp`) builder + validators**, built for an
experimental AI music-composition project.

You describe an arrangement in JSON (tempo, patterns, notes, a few playlist
clips); `flpbuild` writes a real `.flp` that FL Studio can open. The other
scripts validate projects and audio, and extract objective metrics.

- No third-party dependencies for the FLP tools (Python standard library only).
- **No bundled `.flp` templates**: pass `--template` with a project you created.
- `check_audio.py` optionally uses `numpy` / `soundfile` for audio analysis.
- Built against **FL Studio 2026 (26.1.7)**, PPQ 96, `.flp` event-stream format.

> Not affiliated with Image-Line. `.flp` is an Image-Line format; this project is
> an independent, reverse-engineered writer/reader.

## Quick start

```bash
git clone https://github.com/Kyusakino/flpbuild.git
cd flpbuild

# 0) flpbuild needs an FL Studio project to use as a template.
#    Create one in FL Studio (empty project with the channels/instruments you
#    want), save it, then pass it with --template.
#
# 1) JSON arrangement -> FLP + MIDI
python3 tools/flpbuild.py \
    --spec examples/example_spec.json \
    --template /path/to/your-template.flp \
    --out /tmp/demo/track.flp \
    --midi /tmp/demo/track.mid

# 2) validate the project (structure / note count / duration / optional BPM)
python3 tools/check_entry.py /tmp/demo/track.flp

# 3) objective metrics
python3 tools/metrics.py /tmp/demo/track.flp
```

Open `/tmp/demo/track.flp` in FL Studio.

## Spec format

```json
{
  "bpm": 120,
  "channel_names": ["Piano", "Bass", "Drums"],
  "patterns": [
    {
      "name": "Piano",
      "channel": 0,
      "notes": [[1, 1, 2, 60, 100], [1, 3, 2, 64, 96]]
    }
  ],
  "arrangement": [
    {"pattern": 0, "bar": 1, "bars": 32, "track": 1}
  ]
}
```

| Field | Meaning |
|---|---|
| `bpm` | Project tempo (20–400) |
| `channel_names` | Optional channel names, in channel order |
| `patterns[].name` | Pattern name written into the project |
| `patterns[].channel` | 0-based channel index in the template |
| `patterns[].notes` | `[bar, beat, length_beats, midi_key, velocity]` |
| `arrangement` | Playlist clips: `pattern`, start `bar` (1-based), `bars` length, 1-based `track` |

Coordinates:

- `bar` and `beat` are **1-based**, 4/4 time.
- `length_beats` is in beats (`1` = quarter note, `0.5` = eighth note).
- `midi_key` uses MIDI numbering (`60` = C4, `69` = A4), `1–127` velocity.
- Song seconds = `total_bars * 4 * 60 / bpm`.

`flpbuild` writes notes, pattern names, playlist clips, channel names and
tempo. It does **not** program instruments, mixer tracks, or effect plugins:
those come from the template.

## Templates (not shipped)

`flpbuild` writes into an existing project; it does not create instruments or
plugins from scratch. Provide a template:

1. Open FL Studio, start a new project.
2. Add the channels/instruments and (optionally) effect chains you want.
3. Save the project as `.flp` (e.g. `my-template.flp`).
4. Pass it to `flpbuild --template my-template.flp`.

The writer keeps the template's channels, plugin state, mixer state and pattern
slots, then replaces notes, pattern names, playlist clips, channel names and
tempo. `patterns[].channel` refers to the template's channel order (0-based).

**No `.flp` templates are redistributed in this repository** — FL Studio
project files belong to their creators. The templates used internally
(11 native channels, and a 16-channel variant with BBCSO) are not included.

## Tools

| File | Purpose |
|---|---|
| `tools/flpkit.py` | Minimal `.flp` reader/writer: event stream, patterns, notes, clips, names, tempo |
| `tools/flpbuild.py` | JSON arrangement → `.flp` (+ optional `.mid`) |
| `tools/check_entry.py` | Project validator: structure, note count, duration, BPM, playlist sanity |
| `tools/check_audio.py` | Audio validator: decode/duration/silence/BPM estimate (optional numpy + soundfile) |
| `tools/metrics.py` | Objective metrics: note/pattern/channel counts, density, polyphony, pitch, velocity, structure curve |

Run any script with `--help` for details.

Competition-operations tooling (round timers, scoreboards, anonymization,
submission metadata checks) is intentionally **not** included in this repository.

## FLP format notes (FL Studio 2026, 26.1.7)

These are the facts the writer/reader relies on, verified against files saved by
FL Studio 2026 on macOS:

- File = `FLhd` header chunk (`u32` length 6, then `u16` format/channels/ppq)
  followed by `FLdt` (`u32` body length **including** the 152-byte project-info
  prefix) and the event stream.
- Event id `< 64` → 1-byte payload, `< 128` → 2 bytes, `< 192` → 4 bytes,
  `>= 192` → LEB128 length + data.
- Project info prefix starts with event `199` (version string); the main event
  stream begins at the `237` timestamp event. One event (`172`) carries an extra
  byte, hence offset-based parsing is unreliable — the reader locates the stream
  by scanning for a valid `237` event followed by pattern events.
- Events used by this project: `65` pattern new (written twice per pattern),
  `160` channel IID, `192` channel name (UTF-16LE), `193` pattern name,
  `224` notes (24-byte records), `233` playlist clips (88-byte records),
  `237` timestamp, `238` playlist track, `156` tempo (`u32` BPM × 1000).
- PPQ 96 is what the templates use; the writer follows the template's PPQ.

## Tests

```bash
python3 -m unittest discover -s tools/tests -v
```

The regression test checks the playlist writer against a record captured in
FL Studio's Playlist, independent of the writer itself. Template-dependent
tests are skipped when no local template is present.

## Limitations

- Requires a template project; no bundled `.flp` files.
- No mixer / effect / automation writing (see template note above).
- No audio rendering; use FL Studio or an external renderer.
- No MIDI import; MIDI export only (`--midi`).
- Validators check binary structure and musical constraints, not whether a
  specific FL Studio build opens the file. Always verify in FL Studio.
- Format details may change between FL Studio versions; this targets 2026.x.

## License

MIT — see [LICENSE](LICENSE).
