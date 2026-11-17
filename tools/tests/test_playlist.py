"""Regression tests against a native record verified in FL Studio's Playlist."""
import contextlib
import io
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from flpkit import FLP, Event, Note, encode_text
from check_entry import analyse, check, parse_flp, validate_playlist
from flpbuild import build

# Captured from the GLM repair visibly checked at bars 1 and 61 in FL Studio.
# This fixture is independent of the new writer and is not computed by it.
NATIVE_60_BARS = bytes.fromhex(
    '0000000000500150005a0000f3010000780040004064808000000000005a0000'
    '0100000000000000000000000000000000000000000000000000000000000000'
    '000000000000f03f00000000ffffffff0000000000000000')


def project(ids=(1,)):
    prefix = b'\xc7\x0c26.1.0.5528\0\x9f' + struct.pack('<I', 5528) + b'\x1c\x01'
    events = [Event(237, bytes(16)), Event(156, struct.pack('<I', 90000))]
    for iid in ids:
        events += [Event(65, struct.pack('<H', iid)), Event(223, b''),
                   Event(224, Note(0, 96, 60, 100).to_bytes())]
    for iid in ids:
        events += [Event(65, struct.pack('<H', iid)), Event(193, encode_text(f'Pattern {iid}'))]
    events += [Event(99, bytes(2)), Event(233, b'')]
    return FLP(0, 1, 96, events, prefix)


def payload(p):
    return next(e.payload for e in p.events if e.id == 233)


class NativePlaylistTests(unittest.TestCase):
    def test_empty_playlist_matches_verified_native_fixture(self):
        p = project()
        tail = p.prefix[19:]
        p.set_playlist_clips([dict(position=0, pattern=0, length=23040, track=1)])
        self.assertEqual(payload(p), NATIVE_60_BARS)
        self.assertEqual(p.prefix[19:], tail)
        self.assertEqual(p.version, '26.1.7.5421')
        self.assertEqual(struct.unpack_from('<I', p.prefix, 15)[0], 5421)

    def test_sparse_native_pattern_ids_are_not_list_indices(self):
        p = project((3, 9))
        p.set_playlist_clips([dict(position=384, pattern=1, length=768, track=500)])
        clip = p.playlist_clips()[0]
        self.assertEqual((clip['pattern'], clip['pattern_id'], clip['track']), (1, 9, 500))
        self.assertEqual(struct.unpack_from('<H', payload(p), 6)[0], 20489)
        self.assertEqual(validate_playlist([(e.id, e.payload) for e in p.events])[1], [])

    def test_repeated_pattern_delayed_entry_and_trim_offsets(self):
        p = project()
        p.set_playlist_clips([dict(position=0, pattern=0, length=3072, track=1),
                              dict(position=6144, pattern=0, length=1536, track=2, start_offset=384)])
        clips = p.playlist_clips()
        self.assertEqual([(c['position_ticks'], c['length_ticks'], c['start_offset_ticks'], c['end_offset_ticks'])
                          for c in clips], [(0, 3072, 0, 3072), (6144, 1536, 384, 1920)])
        self.assertNotEqual(payload(p)[32:36], payload(p)[120:124])

    def test_long_clip_keeps_intentional_trailing_silence(self):
        p = project()
        p.set_playlist_clips([dict(position=0, pattern=0, length=23040, track=1)])
        self.assertEqual(max(n.position + n.length for n in p.patterns()[0].notes), 96)
        self.assertEqual(p.playlist_clips()[0]['end_offset_ticks'], 23040)

    def test_new_records_do_not_inherit_unrelated_clip_trims(self):
        p = project()
        next(e for e in p.events if e.id == 233).payload = bytes(88)
        p.set_playlist_clips([dict(position=0, pattern=0, length=23040, track=1)])
        self.assertEqual(payload(p), NATIVE_60_BARS)

    def test_invalid_inputs_fail_without_mutating_project(self):
        cases = [{'position': -1}, {'pattern': True}, {'pattern': 1}, {'length': 0},
                 {'length': 1.5}, {'track': 0}, {'track': 501},
                 {'position': 0xFFFFFFFF, 'length': 2}, {'start_offset': 0xFFFFFFFF}]
        for change in cases:
            with self.subTest(change=change):
                p = project()
                before = p.to_bytes()
                clip = dict(position=0, pattern=0, length=384, track=1)
                clip.update(change)
                with self.assertRaises(ValueError):
                    p.set_playlist_clips([clip])
                self.assertEqual(p.to_bytes(), before)

    def test_old_writer_corruption_is_rejected_by_both_validators(self):
        old = bytearray(88)
        struct.pack_into('<IHHIH', old, 0, 0, 0, 20480, 23040, 499)
        p = project()
        next(e for e in p.events if e.id == 233).payload = bytes(old)
        with self.assertRaisesRegex(ValueError, '基准值'):
            p.playlist_clips()
        errors = validate_playlist([(e.id, e.payload) for e in p.events])[1]
        self.assertTrue(any('基准值' in e for e in errors))
        self.assertTrue(any('原生标记' in e for e in errors))

    def test_corrupt_native_fields_are_rejected(self):
        for name, offset, fmt, value in [('pattern', 6, '<H', 20482),
                                          ('zero_based_pattern', 6, '<H', 20480),
                                          ('track', 12, '<H', 500),
                                          ('length', 8, '<I', 0),
                                          ('marker', 16, '<H', 0),
                                          ('end_offset', 28, '<I', 0)]:
            with self.subTest(name=name):
                p = project()
                raw = bytearray(NATIVE_60_BARS)
                struct.pack_into(fmt, raw, offset, value)
                next(e for e in p.events if e.id == 233).payload = bytes(raw)
                with self.assertRaises(ValueError):
                    p.playlist_clips()
                self.assertTrue(validate_playlist([(e.id, e.payload) for e in p.events])[1])

    def test_truncated_record_is_not_silently_skipped(self):
        p = project()
        next(e for e in p.events if e.id == 233).payload = NATIVE_60_BARS[:-1]
        with self.assertRaises(ValueError):
            p.playlist_clips()
        self.assertTrue(validate_playlist([(e.id, e.payload) for e in p.events])[1])

    def test_checker_uses_arrangement_position_and_duration(self):
        p = project()
        p.set_playlist_clips([dict(position=3072, pattern=0, length=19968, track=1)])
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'duration.flp'
            p.save(dest)
            a = analyse(parse_flp(dest))
        self.assertEqual(a['song_bars'], 60)
        self.assertEqual(a['seconds'], 160)
        self.assertEqual(a['note_bars'], .25)

    def test_checker_rejects_notes_with_no_playlist(self):
        p = project()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'unplaced.flp'
            p.save(dest)
            result = check(dest)
        self.assertFalse(result['pass'])
        self.assertTrue(any('没有编排片段' in e for e in result['fail']))

    @unittest.skipUnless(
        all((ROOT / 'templates' / n).is_file()
            for n in ('blank-11ch.flp', 'blank-16ch.flp')),
        'sample templates are not shipped with this repository')
    def test_real_blank_templates_build_without_changes(self):
        for name in ('blank-11ch.flp', 'blank-16ch.flp'):
            with self.subTest(template=name), tempfile.TemporaryDirectory() as tmp:
                template = ROOT / 'templates' / name
                before = template.read_bytes()
                source = FLP.load(template)
                spec = {'bpm': 90, 'patterns': [{'name': 'Late voice', 'channel': 0,
                         'notes': [[9, 1, 1, 60, 100]]}],
                        'arrangement': [{'pattern': 0, 'bar': 1, 'bars': 60, 'track': 1}]}
                dest = Path(tmp) / 'built.flp'
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(build(spec, dest, template, None), 0)
                built = FLP.load(dest)
                self.assertEqual(payload(built), NATIVE_60_BARS)
                self.assertEqual([e.payload for e in source.events if e.id == 213],
                                 [e.payload for e in built.events if e.id == 213])
                self.assertEqual(template.read_bytes(), before)

    def test_template_without_patterns_is_rejected_before_building(self):
        # A project with no pattern events cannot be built into; simulate one
        # without shipping a third-party empty project as a fixture.
        empty = FLP(0, 1, 96, [Event(237, bytes(16)), Event(156, struct.pack('<I', 90000))],
                    b'\xc7\x0c26.1.0.5528\x00')
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'built.flp'
            with mock.patch('flpbuild.FLP.load', return_value=empty):
                with self.assertRaisesRegex(SystemExit, '只有 0 个 pattern'):
                    build({'bpm': 90, 'patterns': [{'channel': 0, 'notes': []}],
                           'arrangement': [{'pattern': 0, 'bar': 1, 'bars': 60}]},
                          dest, Path('unused.flp'), None)
            self.assertFalse(dest.exists())


if __name__ == '__main__':
    unittest.main()
