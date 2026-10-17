"""Offline tests for the disabled artifact-journal preparation (CSL-255).

Everything here is synthetic. The fixture is not authentic GitHub data, and a
passing suite proves only that the offline validator fails closed as specified.
It does not observe GitHub API or runtime behaviour and does not approve any
candidate contract or artifact trust.

Run: python -X utf8 -B tools/test_candidate_artifact_journal.py
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import importlib
import json
import ast
import os
import sys
import unittest
import warnings
import zipfile
import zlib
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
aj = importlib.import_module('candidate_artifact_journal')

FIXTURE_PATH = ROOT / 'tools/fixtures/candidate-artifact-journal-v1.json'
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
PLACEHOLDER = FIXTURE['archive']['digest_placeholder']
POLICY = FIXTURE['trusted_policy']
ENTRY = POLICY['journal_entry']
OTHER_SHA = 'b' * 40
QUALIFIED = POLICY['repository'] + '/' + POLICY['workflow_path'] + '@refs/heads/main'


def build_archive(entry, payload, *, compress=zipfile.ZIP_STORED, extra=(), comment=b'', symlink=False):
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')  # duplicate-name warnings are the point of some cases
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.comment = comment
            for name, data in ((entry, payload), *extra):
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = compress
                info.create_system = 3
                info.external_attr = (0o120777 if symlink else 0o100644) << 16
                archive.writestr(info, data)
    return buffer.getvalue()


def set_flag_bits(archive, mask):
    data = bytearray(archive)
    for signature, offset in ((b'PK\x03\x04', 6), (b'PK\x01\x02', 8)):
        start = 0
        while (at := data.find(signature, start)) >= 0:
            pos = at + offset
            data[pos:pos + 2] = (int.from_bytes(data[pos:pos + 2], 'little') | mask).to_bytes(2, 'little')
            start = at + 4
    return bytes(data)


def entry_sizes(archive):
    """(compress_size, file_size) of the single entry as the central directory declares them."""
    with zipfile.ZipFile(io.BytesIO(archive)) as container:
        (info,) = container.infolist()
        return info.compress_size, info.file_size


def compressed_span(archive):
    """[start, end) of the single entry's raw compressed bytes inside a build_archive() result.

    Local file header: compressed size at offset 18, name length at 26, extra length at 28,
    data at 30 + name + extra. build_archive writes one entry at offset 0 with no data
    descriptor, so the header sizes are authoritative.
    """
    assert archive[:4] == b'PK\x03\x04'
    compress_size = int.from_bytes(archive[18:22], 'little')
    start = 30 + int.from_bytes(archive[26:28], 'little') + int.from_bytes(archive[28:30], 'little')
    return start, start + compress_size


def set_compress_size(archive, value):
    """Rewrite the declared compressed size in the local header (offset 18) and the central
    directory record (offset 20) of a single-entry archive; the data bytes stay in place."""
    data = bytearray(archive)
    data[18:22] = value.to_bytes(4, 'little')
    at = data.find(b'PK\x01\x02')
    assert at > 0
    data[at + 20:at + 24] = value.to_bytes(4, 'little')
    return bytes(data)


class Case:
    """One materialized synthetic scenario. Mutate ``before``/``document``, then evaluate."""

    def __init__(self):
        fixture = copy.deepcopy(FIXTURE)
        self.policy = fixture['trusted_policy']
        self.document = fixture['journal_document']
        self.candidate = fixture['candidate']
        self.previous = fixture['previous_evidence']
        self.before = fixture['snapshot']
        self.after_observed_at = fixture['snapshot_after_observed_at']
        self.set_document(self.document)

    def set_document(self, document):
        self.document = document
        self.set_archive(build_archive(self.policy['journal_entry'], aj.canonical_bytes(document)))

    def set_archive(self, archive):
        self.archive = archive
        meta = self.artifact()
        meta['digest'] = 'sha256:' + hashlib.sha256(archive).hexdigest()
        meta['size_in_bytes'] = len(archive)

    def artifact(self):
        return next(m for m in self.before['artifacts'] if m['name'] == self.policy['artifact_name'])

    def run(self):
        return self.before['run']

    def mutate_run(self, **changes):
        for target in (self.before['run'], *[e for e in self.before['workflow_runs'] if e['id'] == self.before['run']['id']]):
            for key, value in changes.items():
                if isinstance(value, dict) and isinstance(target.get(key), dict):
                    target[key].update(value)
                else:
                    target[key] = value

    def evaluate(self, sync=True, after=None):
        if sync:
            after = copy.deepcopy(self.before)
            after['observed_at'] = self.after_observed_at
        return aj.evaluate_offline(self.policy, self.before, after, self.archive, self.candidate, self.previous)


def successor(previous_document, *appended, **changes):
    current = copy.deepcopy(previous_document)
    current['journal_sequence'] = previous_document['journal_sequence'] + 1
    current['predecessor_digest'] = aj.canonical_digest(previous_document)
    current['records'] = copy.deepcopy(previous_document['records']) + list(appended)
    current.update(changes)
    return current


def record(sequence, operation_id, candidate, state, operation='label-post', run_id=300000001):
    return {'sequence': sequence, 'operation_id': operation_id, 'candidate': candidate,
            'operation': operation, 'state': state, 'run_id': run_id}


class FixtureTests(unittest.TestCase):
    def test_fixture_is_declared_synthetic_and_validates_without_authority(self):
        self.assertIn('synthetic', FIXTURE['notice'].lower())
        self.assertIn('non-authentic', FIXTURE['notice'].lower())
        case = Case()
        result = case.evaluate()
        expected = FIXTURE['expected']
        self.assertTrue(result['diagnostic_valid'])
        for key in ('chain', 'semantic_state', 'candidate_state', 'operator_reconciliation_required',
                    'pr_unresolved_operations'):
            self.assertEqual(result[key], expected[key], key)
        self.assertIs(result['admitted'], False)
        self.assertIs(result['dispatch_allowed'], False)
        self.assertEqual(result['side_effects'], 'rejected')
        self.assertEqual(result['stage'], aj.STAGE)
        self.assertEqual(result['run_id'], case.run()['id'])
        self.assertEqual(result['artifact_id'], case.artifact()['id'])
        self.assertEqual(result['journal_digest'], aj.canonical_digest(case.document))
        self.assertEqual(json.loads(json.dumps(result))['dispatch_allowed'], False)

    def test_placeholders_never_validate(self):
        case = Case()
        meta = case.artifact()
        meta['digest'], meta['size_in_bytes'] = PLACEHOLDER, FIXTURE['archive']['size_placeholder']
        with self.assertRaises(aj.ProvenanceError):
            case.evaluate()

    def test_candidate_key_matches_neutral_journal_vector(self):
        candidates = [FIXTURE['candidate']] + [v['candidate'] for v in FIXTURE['other_candidates'].values()]
        self.assertEqual(aj.candidate_key(candidates[0]),
                         '17166515919c87a770c6583a6dcc74dd45cbc639a9d5272243ec386604212cfa')
        for field in ('head', 'base', 'merge', 'tree'):
            self.assertNotEqual(aj.candidate_key({**candidates[0], field: OTHER_SHA}), aj.candidate_key(candidates[0]))

    def test_other_candidates_have_distinct_semantic_states(self):
        for name, expected in FIXTURE['other_candidates'].items():
            with self.subTest(name=name):
                status = aj.journal_status(FIXTURE['journal_document'], expected['candidate'])
                self.assertEqual(status['semantic_state'], expected['semantic_state'])
                self.assertEqual(status['candidate_state'], expected['semantic_state'])
                self.assertFalse(status['operator_reconciliation_required'])
                self.assertEqual(status['pr_unresolved_operations'], [])
        # A candidate from another repository never reaches semantic evaluation.
        case = Case()
        case.candidate = {**case.candidate, 'repo': 'example-org/fork'}
        with self.assertRaises(aj.JournalError):
            case.evaluate()

    def test_workflow_dispatch_by_reviewed_actor_is_also_diagnostically_valid(self):
        case = Case()
        case.mutate_run(event='workflow_dispatch')
        case.document['producer']['event'] = 'workflow_dispatch'
        case.set_document(case.document)
        self.assertTrue(case.evaluate()['diagnostic_valid'])


class ArchiveIntegrityTests(unittest.TestCase):
    def test_altered_bytes_digest_or_size_fail(self):
        case = Case()
        meta = case.artifact()
        flipped = bytearray(case.archive)
        flipped[-1] ^= 0xFF
        with self.assertRaises(aj.IntegrityError):
            aj.verify_archive(bytes(flipped), meta, case.policy)
        for change in ({'digest': 'sha256:' + 'f' * 64}, {'size_in_bytes': len(case.archive) + 1},
                       {'size_in_bytes': len(case.archive) - 1}):
            with self.subTest(change=change), self.assertRaises(aj.IntegrityError):
                aj.verify_archive(case.archive, {**meta, **change}, case.policy)
        with self.assertRaises(aj.IntegrityError):
            aj.verify_archive(case.archive.decode('latin-1'), meta, case.policy)

    def test_crc_is_checked_independently_of_the_server_digest(self):
        case = Case()
        corrupted = bytearray(case.archive)
        at = corrupted.find(b'{"')
        self.assertGreater(at, 0)
        corrupted[at + 1] ^= 0x01
        case.set_archive(bytes(corrupted))  # digest now honestly matches the corrupted bytes
        with self.assertRaises(aj.IntegrityError):
            case.evaluate()

    def test_bounded_zip_structure(self):
        payload = aj.canonical_bytes(FIXTURE['journal_document'])
        bad = {
            'duplicate path': build_archive(ENTRY, payload, extra=[(ENTRY, payload)]),
            'second entry': build_archive(ENTRY, payload, extra=[('notes.txt', b'x')]),
            'wrong entry name': build_archive('other.json', payload),
            'traversal': build_archive('../' + ENTRY, payload),
            'nested path': build_archive('dir/' + ENTRY, payload),
            'absolute': build_archive('/' + ENTRY, payload),
            'directory': build_archive(ENTRY + '/', b''),
            'symlink': build_archive(ENTRY, payload, symlink=True),
            'encrypted flag': set_flag_bits(build_archive(ENTRY, payload), 0x1),
            'strong encryption flag': set_flag_bits(build_archive(ENTRY, payload), 0x40),
            'archive comment': build_archive(ENTRY, payload, comment=b'trusted=true'),
            'not a zip': b'PK\x03\x04' + b'\x00' * 40,
            'empty': b'',
            'empty entry': build_archive(ENTRY, b''),
            'suspicious ratio': build_archive(ENTRY, b'0' * 100000, compress=zipfile.ZIP_DEFLATED),
            'oversize entry': build_archive(ENTRY, b'0' * (POLICY['bounds']['max_entry_bytes'] + 1)),
        }
        try:
            bad['bzip2'] = build_archive(ENTRY, payload, compress=zipfile.ZIP_BZIP2)
        except RuntimeError:
            pass  # compression module unavailable on this interpreter; other cases still run
        for name, archive in bad.items():
            with self.subTest(name=name):
                case = Case()
                case.set_archive(archive)
                # Layer 1 in isolation: honest metadata for these bytes still rejects as integrity.
                with self.assertRaises(aj.IntegrityError):
                    aj.verify_archive(archive, case.artifact(), case.policy)
                # End to end, layers run in order. Zero-byte authenticated metadata is a
                # provenance rejection before any ZIP check; every other case reaches layer 1.
                end_to_end = aj.ProvenanceError if name == 'empty' else aj.IntegrityError
                with self.assertRaises(end_to_end):
                    case.evaluate()
        case = Case()
        case.set_archive(build_archive(ENTRY, b'0' * (POLICY['bounds']['max_archive_bytes'] + 1)))
        with self.assertRaises(aj.EvidenceError):
            case.evaluate()

    def test_deflated_valid_entry_is_accepted(self):
        case = Case()
        case.set_archive(build_archive(ENTRY, aj.canonical_bytes(case.document), compress=zipfile.ZIP_DEFLATED))
        self.assertTrue(case.evaluate()['diagnostic_valid'])

    def test_corrupted_deflate_stream_is_an_integrity_error(self):
        # The decompressor, not the ZIP layer, rejects invalid raw deflate bytes: zlib.error is
        # not a BadZipFile, so testzip() does not translate it. The fixture keeps every header
        # field intact and carries honest size/digest metadata, so the only failing layer is
        # the decompressor, and the result must be the IntegrityError family, not an escape.
        intact = build_archive(ENTRY, aj.canonical_bytes(FIXTURE['journal_document']), compress=zipfile.ZIP_DEFLATED)
        start, end = compressed_span(intact)
        self.assertGreater(end - start, 8)
        # Deterministic: BTYPE=11 in the first deflate block header is a reserved block type.
        corrupted = bytearray(intact)
        corrupted[start] |= 0x06
        corrupted = bytes(corrupted)
        self.assertEqual(entry_sizes(corrupted), entry_sizes(intact))
        case = Case()
        case.set_archive(corrupted)  # authenticated metadata honestly matches the corrupted bytes
        with self.assertRaises(aj.IntegrityError) as ctx:
            aj.verify_archive(corrupted, case.artifact(), case.policy)
        self.assertIn('entry could not be read', str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, zlib.error)
        with self.assertRaises(aj.IntegrityError):
            case.evaluate()
        # Every single-byte corruption of the compressed data fails closed as integrity,
        # whichever of the decompressor or the CRC check notices first. The final byte is
        # treated separately: it may carry only trailing padding bits after the end-of-block
        # code, in which case the stream still decodes to the identical bytes with a matching
        # CRC, and identical content is not an integrity defect.
        for position in range(start, end - 1):
            with self.subTest(position=position - start):
                flipped = bytearray(intact)
                flipped[position] ^= 0xFF
                flipped = bytes(flipped)
                case = Case()
                case.set_archive(flipped)
                with self.assertRaises(aj.IntegrityError):
                    aj.verify_archive(flipped, case.artifact(), case.policy)
                with self.assertRaises(aj.IntegrityError):
                    case.evaluate()
        final = bytearray(intact)
        final[end - 1] ^= 0xFF
        final = bytes(final)
        case = Case()
        case.set_archive(final)
        try:
            parsed = aj.verify_archive(final, case.artifact(), case.policy)
        except aj.IntegrityError:
            pass
        else:
            self.assertEqual(parsed, FIXTURE['journal_document'])
        # The digest/size layer still runs first: the same corruption with the intact
        # metadata is a digest mismatch, not a decompressor report.
        case = Case()
        case.set_archive(intact)
        with self.assertRaises(aj.IntegrityError) as ctx:
            aj.verify_archive(corrupted, case.artifact(), case.policy)
        self.assertIn('sha256', str(ctx.exception))

    def test_compression_ratio_is_measured_against_actual_compressed_size(self):
        limit = aj.MAX_COMPRESSION_RATIO
        self.assertEqual(limit, 32)

        def deflated(n):
            # A strict JSON object whose only value is a run of n identical characters.
            return build_archive(ENTRY, b'{"k":"' + b'a' * n + b'"}', compress=zipfile.ZIP_DEFLATED)

        def layer1(archive):
            case = Case()
            case.set_archive(archive)
            return case, aj.verify_archive(archive, case.artifact(), case.policy)

        def rejects(archive, fragment):
            case = Case()
            case.set_archive(archive)
            with self.assertRaises(aj.IntegrityError) as ctx:
                aj.verify_archive(archive, case.artifact(), case.policy)
            self.assertIn(fragment, str(ctx.exception))
            with self.assertRaises(aj.IntegrityError):
                case.evaluate()

        # Small entry, modest ratio: admitted at layer 1 (the parsed object comes back) and
        # rejected only later as a journal, proving the ratio check did not fire.
        modest = deflated(40)
        c, f = entry_sizes(modest)
        self.assertLess(c, 64)
        self.assertLessEqual(f, limit * c)
        case, parsed = layer1(modest)
        self.assertEqual(parsed, {'k': 'a' * 40})
        with self.assertRaises(aj.JournalError):
            case.evaluate()

        # Exact boundary, found by search rather than by assuming compressor output bytes:
        # a run length n where file_size == 32 * compress_size and n + 1 keeps the same
        # compress_size. The compressed size of a run grows far slower than the run, so the
        # difference file_size - 32 * compress_size walks through zero within a few hundred
        # bytes for any deflate implementation.
        boundary = None
        for n in range(1, 4096):
            c, f = entry_sizes(deflated(n))
            if f == limit * c and entry_sizes(deflated(n + 1))[0] == c:
                boundary = n
                break
        self.assertIsNotNone(boundary, 'no exact ratio boundary found for a repeated-byte run')
        at_limit, over_limit = deflated(boundary), deflated(boundary + 1)
        c, f = entry_sizes(at_limit)
        self.assertEqual(f, limit * c)
        # The former max(compress_size, 64) floor would have admitted both of these.
        self.assertLess(c, 64)
        self.assertLessEqual(f + 1, limit * 64)
        case, parsed = layer1(at_limit)
        self.assertEqual(parsed, {'k': 'a' * boundary})
        with self.assertRaises(aj.JournalError):
            case.evaluate()
        c_over, f_over = entry_sizes(over_limit)
        self.assertEqual((c_over, f_over), (c, f + 1))
        rejects(over_limit, 'suspicious compression ratio')

        # Well over the limit for a small entry that is still far below 32 * 64 bytes.
        small_bomb = deflated(1500)
        c, f = entry_sizes(small_bomb)
        self.assertLess(c, 64)
        self.assertGreater(f, limit * c)
        self.assertLessEqual(f, limit * 64)
        rejects(small_bomb, 'suspicious compression ratio')

        # Stored entries are ratio 1 and unaffected.
        stored = build_archive(ENTRY, b'{"k":"' + b'a' * 1500 + b'"}')
        self.assertEqual(entry_sizes(stored), (1508, 1508))
        self.assertEqual(layer1(stored)[1], {'k': 'a' * 1500})

    def test_zero_compressed_size_for_nonempty_entry_rejects(self):
        payload = aj.canonical_bytes(FIXTURE['journal_document'])
        for name, archive in (('deflated', build_archive(ENTRY, payload, compress=zipfile.ZIP_DEFLATED)),
                              ('stored', build_archive(ENTRY, payload))):
            with self.subTest(name=name):
                zeroed = set_compress_size(archive, 0)
                c, f = entry_sizes(zeroed)
                self.assertEqual(c, 0)
                self.assertEqual(f, len(payload))
                case = Case()
                case.set_archive(zeroed)
                with self.assertRaises(aj.IntegrityError) as ctx:
                    aj.verify_archive(zeroed, case.artifact(), case.policy)
                self.assertIn('zero compressed size', str(ctx.exception))
                with self.assertRaises(aj.IntegrityError):
                    case.evaluate()
        # Understating a nonzero compressed size is a ratio rejection, never a partial read.
        archive = build_archive(ENTRY, payload, compress=zipfile.ZIP_DEFLATED)
        c, f = entry_sizes(archive)
        understated = set_compress_size(archive, max(1, (f - 1) // aj.MAX_COMPRESSION_RATIO))
        self.assertLess(entry_sizes(understated)[0], c)
        case = Case()
        case.set_archive(understated)
        with self.assertRaises(aj.IntegrityError) as ctx:
            aj.verify_archive(understated, case.artifact(), case.policy)
        self.assertIn('suspicious compression ratio', str(ctx.exception))

    def test_strict_json(self):
        base = aj.canonical_bytes(FIXTURE['journal_document'])
        self.assertEqual(aj.parse_strict_json(base + b'\n', len(base) + 1), FIXTURE['journal_document'])
        with self.assertRaises(aj.IntegrityError):
            aj.parse_strict_json(base, len(base) - 1)
        bad = {
            'duplicate key': base[:-1] + b',"schema":"candidate-artifact-journal/v1"}',
            'duplicate nested key': base.replace(b'"run_attempt":1', b'"run_attempt":1,"run_attempt":1', 1),
            'NaN': base[:-1] + b',"extra":NaN}',
            'Infinity': base[:-1] + b',"extra":-Infinity}',
            'array': b'[]',
            'scalar': b'1',
            'bom': b'\xef\xbb\xbf' + base,
            'two documents': base + base,
            'invalid utf8': base[:-1] + b',"x":"\xff"}',
            # Object nesting, so the root-object check is not the only rejection path.
            'deep nesting': b'{"a":' * 5000 + b'1' + b'}' * 5000,
            'truncated': base[:-5],
        }
        for name, payload in bad.items():
            with self.subTest(name=name):
                with self.assertRaises(aj.IntegrityError):
                    aj.parse_strict_json(payload, 1 << 20)
                case = Case()
                case.set_archive(build_archive(ENTRY, payload))
                with self.assertRaises(aj.IntegrityError):
                    case.evaluate()

    def test_json_nesting_bound(self):
        # Convention: the root object is depth 1; every nested object or array adds 1.
        limit = aj.MAX_JSON_DEPTH
        self.assertEqual(limit, 64)

        def objects(depth):
            return b'{"a":' * depth + b'1' + b'}' * depth

        def mixed(depth):
            body = b'1'
            for level in range(depth, 0, -1):
                body = (b'{"a":' + body + b'}') if level % 2 else (b'[' + body + b']')
            return body

        def lists_under_root(depth):
            return b'{"a":' + b'[' * (depth - 1) + b'1' + b']' * (depth - 1) + b'}'

        def empty_leaf(depth):
            return b'{"a":' * (depth - 1) + b'{}' + b'}' * (depth - 1)

        def wide_shallow():
            # Many siblings at depth 2 must not accumulate into depth.
            return b'{' + b','.join(b'"k%d":[1]' % i for i in range(limit * 4)) + b'}'

        accepted = {
            'objects at limit': objects(limit),
            'mixed at limit': mixed(limit),
            'lists under root at limit': lists_under_root(limit),
            'empty container at limit': empty_leaf(limit),
            'one below limit': objects(limit - 1),
            'wide but shallow': wide_shallow(),
        }
        for name, payload in accepted.items():
            with self.subTest(name=name):
                self.assertIsInstance(aj.parse_strict_json(payload, 1 << 20), dict)

        rejected = {
            'objects one over limit': objects(limit + 1),
            'mixed one over limit': mixed(limit + 1),
            'lists under root one over limit': lists_under_root(limit + 1),
            'empty container one over limit': empty_leaf(limit + 1),
            'deep sibling after shallow siblings': b'{"a":1,"b":' + mixed(limit) + b'}',
        }
        for name, payload in rejected.items():
            with self.subTest(name=name):
                with self.assertRaises(aj.IntegrityError) as ctx:
                    aj.parse_strict_json(payload, 1 << 20)
                self.assertIn('nesting exceeds bound', str(ctx.exception))
                case = Case()
                case.set_archive(build_archive(ENTRY, payload))
                with self.assertRaises(aj.IntegrityError):
                    case.evaluate()

        # The shipped fixture is well inside the bound.
        aj.parse_strict_json(aj.canonical_bytes(FIXTURE['journal_document']), 1 << 20)


class JournalEnvelopeTests(unittest.TestCase):
    def rejects(self, mutate, error=aj.JournalError):
        case = Case()
        mutate(case.document)
        case.set_document(case.document)
        with self.assertRaises(error):
            case.evaluate()

    def test_unknown_fields_and_trust_flags_are_rejected(self):
        for name, mutate in {
            'envelope trusted flag': lambda d: d.update(trusted=True),
            'attestation claim': lambda d: d.update(attestation='verified'),
            'producer trusted flag': lambda d: d['producer'].update(trusted=True),
            'record note': lambda d: d['records'][0].update(note='x'),
            'missing predecessor field': lambda d: d.pop('predecessor_digest'),
            'missing records': lambda d: d.pop('records'),
            'wrong schema': lambda d: d.update(schema='candidate-artifact-journal/v2'),
        }.items():
            with self.subTest(name=name):
                self.rejects(mutate)

    def test_envelope_must_bind_the_trusted_policy_not_assert_it(self):
        for field, bad in (('repository_id', POLICY['repository_id'] + 1), ('repository_id', True),
                           ('repository', 'example-org/fork'), ('policy_commit', OTHER_SHA),
                           ('workflow_id', POLICY['workflow_id'] + 1), ('workflow_path', QUALIFIED),
                           ('workflow_path', '.github/workflows/other.yml')):
            with self.subTest(field=field, bad=bad):
                self.rejects(lambda d: d.update({field: bad}))
        # Deriving the policy from the envelope's own assertions fails at provenance:
        # the independent snapshot still names the real default-branch commit.
        case = Case()
        case.document['policy_commit'] = OTHER_SHA
        case.document['producer']['runtime_sha'] = OTHER_SHA
        case.policy['policy_commit'] = OTHER_SHA
        case.policy['source_closure']['commit'] = OTHER_SHA
        case.set_document(case.document)
        with self.assertRaises(aj.ProvenanceError):
            case.evaluate()

    def test_producer_binding_rejects_forged_or_foreign_run_identity(self):
        run_id = FIXTURE['snapshot']['run']['id']
        for name, change in {
            'foreign run id': {'run_id': run_id + 1},
            'older run id': {'run_id': run_id - 1},
            'rerun attempt': {'run_attempt': 2},
            'bool attempt': {'run_attempt': True},
            'candidate event': {'event': 'pull_request'},
            'push event': {'event': 'push'},
            'runtime sha is candidate merge': {'runtime_sha': FIXTURE['candidate']['merge']},
            'runtime sha is candidate head': {'runtime_sha': FIXTURE['candidate']['head']},
            'bare workflow ref': {'workflow_ref': POLICY['workflow_path']},
            'foreign repo workflow ref': {'workflow_ref': 'example-org/fork/' + POLICY['workflow_path'] + '@refs/heads/main'},
            'other ref': {'workflow_ref': QUALIFIED.replace('refs/heads/main', 'refs/heads/feature')},
            'sha ref': {'workflow_ref': QUALIFIED.replace('refs/heads/main', POLICY['policy_commit'])},
        }.items():
            with self.subTest(name=name):
                self.rejects(lambda d: d['producer'].update(change))

    def test_record_rules(self):
        X, Y = FIXTURE['candidate'], FIXTURE['other_candidates']['confirmed']['candidate']
        A = FIXTURE['journal_document']['records'][0]['operation_id']

        def swap(d):
            d['records'][0], d['records'][1] = d['records'][1], d['records'][0]

        def first_state_attempted(d):
            # Resequenced so contiguity holds and only the transition check can reject.
            d['records'].pop(0)
            for i, r in enumerate(d['records'], start=1):
                r['sequence'] = i

        for name, mutate in {
            'sequence gap': lambda d: d['records'][1].update(sequence=3),
            'reordered records': swap,
            'first state attempted': first_state_attempted,
            'skip attempted': lambda d: d['records'].__setitem__(1, {**d['records'][1], 'state': 'confirmed'}),
            'record after unknown': lambda d: d['records'].append(record(7, A, X, 'confirmed')),
            'record after confirmed': lambda d: d['records'].append(record(7, d['records'][5]['operation_id'], Y, 'attempted')),
            'reserved after rejected': lambda d: d['records'].__setitem__(5, {**d['records'][5], 'state': 'rejected'}) or d['records'].append(record(7, d['records'][5]['operation_id'], Y, 'reserved')),
            'candidate mixup within operation': lambda d: d['records'][1].update(candidate=Y),
            'operation rebound': lambda d: d['records'][1].update(operation='comment-post'),
            'candidate repo differs': lambda d: d['records'][0].update(candidate={**X, 'repo': 'example-org/fork'}),
            'record run newer than producer': lambda d: d['records'][0].update(run_id=d['producer']['run_id'] + 1),
            'unknown operation': lambda d: d['records'][0].update(operation='merge'),
            'unknown state': lambda d: d['records'][0].update(state='done'),
            'short operation id': lambda d: d['records'][0].update(operation_id='abc'),
            'short sha': lambda d: d['records'][0].update(candidate={**X, 'head': 'c1' * 19}),
            'uppercase sha': lambda d: d['records'][0].update(candidate={**X, 'head': 'C1' * 20}),
            'bool pr': lambda d: d['records'][0].update(candidate={**X, 'pr': True}),
        }.items():
            with self.subTest(name=name):
                self.rejects(mutate)
        # Record bound, isolated from the archive entry bound. At the fixture bound the
        # serialized document would exceed max_entry_bytes and reject at layer 1 first, so
        # the layer-3 bound is exercised directly and end to end with a tighter policy bound.
        limit = POLICY['bounds']['max_records']
        many = [record(i + 1, format(i, '032x'), X, 'reserved') for i in range(limit + 1)]
        aj.validate_records(many[:limit], X['repo'], limit, 300000001)
        with self.assertRaises(aj.JournalError):
            aj.validate_records(many, X['repo'], limit, 300000001)
        case = Case()
        case.policy['bounds']['max_records'] = len(case.document['records']) - 1
        with self.assertRaises(aj.JournalError):
            case.evaluate()
        case = Case()
        case.policy['bounds']['max_records'] = len(case.document['records'])
        self.assertTrue(case.evaluate()['diagnostic_valid'])
        # Exhaustive transition table on a single operation.
        for previous, state in [(p, s) for p in (None, *aj.STATES) for s in aj.STATES]:
            records = [record(1, A, X, 'reserved')] if previous else []
            if previous and previous != 'reserved':
                records.append(record(2, A, X, 'attempted'))
            if previous in ('confirmed', 'rejected', 'unknown'):
                records.append(record(3, A, X, previous))
            records.append(record(len(records) + 1, A, X, state))
            allowed = state in aj.TRANSITIONS[previous]
            with self.subTest(previous=previous, state=state):
                if allowed:
                    aj.validate_records(records, X['repo'], 10, 300000001)
                else:
                    with self.assertRaises(aj.JournalError):
                        aj.validate_records(records, X['repo'], 10, 300000001)

    def test_predecessor_rules(self):
        self.rejects(lambda d: d.update(predecessor_digest='0' * 64))
        self.rejects(lambda d: d.update(journal_sequence=2))
        self.rejects(lambda d: d.update(journal_sequence=0, predecessor_digest=None))
        self.rejects(lambda d: d.update(journal_sequence=2, predecessor_digest='0' * 64))  # genesis previous


class ProvenanceTests(unittest.TestCase):
    def test_policy_validation(self):
        closure = POLICY['source_closure']
        bad = {
            'qualified workflow path': {'workflow_path': QUALIFIED},
            'workflow path with ref': {'workflow_path': POLICY['workflow_path'] + '@main'},
            'workflow outside .github/workflows': {'workflow_path': 'scripts/ci/x.yml'},
            'bool workflow id': {'workflow_id': True},
            'closure commit mismatch': {'source_closure': {**closure, 'commit': OTHER_SHA}},
            'closure missing workflow': {'source_closure': {**closure, 'files': {'scripts/ci/x.py': '3' * 64}}},
            'closure traversal': {'source_closure': {**closure, 'files': {**closure['files'], '../x.py': '3' * 64}}},
            'closure absolute': {'source_closure': {**closure, 'files': {**closure['files'], '/etc/x': '3' * 64}}},
            'closure digest short': {'source_closure': {**closure, 'files': {POLICY['workflow_path']: 'abc'}}},
            'closure empty': {'source_closure': {**closure, 'files': {}}},
            'candidate event': {'producer_events': ['pull_request']},
            'no events': {'producer_events': []},
            'duplicate events': {'producer_events': ['schedule', 'schedule']},
            'string actor id': {'dispatch_actor_ids': ['500000001']},
            'duplicate actor id': {'dispatch_actor_ids': [500000001, 500000001]},
            'entry bound above archive bound': {'bounds': {**POLICY['bounds'], 'max_entry_bytes': 1 << 20}},
            'archive bound above ceiling': {'bounds': {**POLICY['bounds'], 'max_archive_bytes': aj.MAX_ARCHIVE_CEILING + 1}},
            'records bound above ceiling': {'bounds': {**POLICY['bounds'], 'max_records': aj.MAX_RECORDS_CEILING + 1}},
            'unknown key': {'trusted': True},
            'bool repository id': {'repository_id': True},
            'repository traversal': {'repository': '../..'},
            'repository dot owner': {'repository': './example-repo'},
            'repository dotdot name': {'repository': 'example-org/..'},
            'short policy commit': {'policy_commit': 'a0' * 19},
            'branch traversal': {'default_branch': 'refs/heads/..'},
            'branch lock': {'default_branch': 'main.lock'},
            'entry with slash': {'journal_entry': 'dir/x.json'},
            'entry not json': {'journal_entry': 'x.txt'},
            'wrong schema': {'schema': 'candidate-artifact-journal-policy/v2'},
        }
        for name, change in bad.items():
            with self.subTest(name=name), self.assertRaises(aj.ProvenanceError):
                aj.validate_policy({**copy.deepcopy(POLICY), **change})
        with self.assertRaises(aj.ProvenanceError):
            aj.validate_policy({k: v for k, v in POLICY.items() if k != 'bounds'})
        # Contract: a valid policy is returned as the same object, unmodified.
        policy = copy.deepcopy(POLICY)
        self.assertIs(aj.validate_policy(policy), policy)
        self.assertEqual(policy, POLICY)

    def test_repository_identity_rejects_dot_segments_at_every_layer(self):
        X = FIXTURE['candidate']
        for bad in ('../..', './.', '../example-repo', './example-repo', 'example-org/..', 'example-org/.',
                    'a/b/c', 'example-repo', '/example-repo', 'example-org/', 'example org/repo'):
            with self.subTest(bad=bad):
                with self.assertRaises(aj.ProvenanceError):  # trusted policy layer
                    aj.validate_policy({**copy.deepcopy(POLICY), 'repository': bad})
                with self.assertRaises(aj.JournalError):  # candidate layer
                    aj.validate_candidate({**X, 'repo': bad})
                with self.assertRaises(aj.JournalError):
                    aj.journal_status(FIXTURE['journal_document'], {**X, 'repo': bad})
                case = Case()  # envelope layer: cannot bind a valid policy
                case.document['repository'] = bad
                case.set_document(case.document)
                with self.assertRaises(aj.JournalError):
                    case.evaluate()
                case = Case()  # record candidate layer inside a valid envelope
                case.document['records'][0]['candidate'] = {**X, 'repo': bad}
                case.set_document(case.document)
                with self.assertRaises(aj.JournalError):
                    case.evaluate()
        # Ordinary names keep validating, including dots that are not a whole segment.
        for ok in ('example-org/example-repo', 'example.org/example.repo', 'ex_org-1/repo..v2', '.github/..dots'):
            with self.subTest(ok=ok):
                aj.validate_policy({**copy.deepcopy(POLICY), 'repository': ok})
                aj.validate_candidate({**X, 'repo': ok})

    def test_run_identity_event_attempt_and_status(self):
        rid = POLICY['repository_id']
        bad = {
            'repository id': {'repository': {'id': rid + 1}},
            'bool repository id': {'repository': {'id': True}},
            'repository name': {'repository': {'full_name': 'example-org/fork'}},
            'fork head repository': {'head_repository': {'id': rid + 1}},
            'pull_request event': {'event': 'pull_request'},
            'pull_request_target event': {'event': 'pull_request_target'},
            'push event': {'event': 'push'},
            'issue_comment event': {'event': 'issue_comment'},
            'rerun attempt 2': {'run_attempt': 2},
            'attempt 0': {'run_attempt': 0},
            'bool attempt': {'run_attempt': True},
            'in progress': {'status': 'in_progress', 'conclusion': None},
            'queued': {'status': 'queued', 'conclusion': None},
            'failure': {'conclusion': 'failure'},
            'cancelled': {'conclusion': 'cancelled'},
            'head sha moved': {'head_sha': OTHER_SHA},
            'head sha is candidate merge': {'head_sha': FIXTURE['candidate']['merge']},
            'feature branch': {'head_branch': 'feature'},
            'workflow id': {'workflow_id': POLICY['workflow_id'] + 1},
            'bool workflow id': {'workflow_id': True},
            'other path': {'path': '.github/workflows/other.yml'},
            'qualified path': {'path': QUALIFIED},
            'bool run id': {'id': True},
            'timestamps reversed': {'created_at': '2026-10-16T11:56:00Z'},
            'naive timestamp': {'updated_at': '2026-10-16T11:55:00'},
        }
        for name, change in bad.items():
            with self.subTest(name=name):
                case = Case()
                case.mutate_run(**change)
                with self.assertRaises(aj.ProvenanceError):
                    case.evaluate()
        case = Case()
        case.before['run']['extra'] = 1
        with self.assertRaises(aj.ProvenanceError):
            case.evaluate()

    def test_dispatch_actor_rules(self):
        reviewed = POLICY['dispatch_actor_ids'][0]
        for name, actor, ok in (
            ('reviewed user', {'id': reviewed, 'login': 'synthetic-owner', 'type': 'User'}, True),
            ('unreviewed user', {'id': reviewed + 1, 'login': 'someone', 'type': 'User'}, False),
            ('bot with reviewed id', {'id': reviewed, 'login': 'bot[bot]', 'type': 'Bot'}, False),
            ('organization', {'id': reviewed, 'login': 'org', 'type': 'Organization'}, False),
        ):
            with self.subTest(name=name):
                case = Case()
                case.mutate_run(event='workflow_dispatch', triggering_actor=actor)
                case.document['producer']['event'] = 'workflow_dispatch'
                case.set_document(case.document)
                if ok:
                    self.assertTrue(case.evaluate()['diagnostic_valid'])
                else:
                    with self.assertRaises(aj.ProvenanceError):
                        case.evaluate()
        # schedule runs do not consult the dispatch list; a policy without dispatch
        # actors still accepts schedule and rejects every dispatch.
        case = Case()
        case.policy['dispatch_actor_ids'] = []
        self.assertTrue(case.evaluate()['diagnostic_valid'])
        case.mutate_run(event='workflow_dispatch')
        case.document['producer']['event'] = 'workflow_dispatch'
        case.set_document(case.document)
        with self.assertRaises(aj.ProvenanceError):
            case.evaluate()
        case = Case()
        case.policy['producer_events'] = ['schedule']
        case.mutate_run(event='workflow_dispatch')
        with self.assertRaises(aj.ProvenanceError):
            case.evaluate()

    def test_latest_run_authority_and_listing_completeness(self):
        def newer(**changes):
            entry = copy.deepcopy(FIXTURE['snapshot']['run'])
            entry.update(id=entry['id'] + 1, created_at='2026-10-16T11:58:00Z', updated_at='2026-10-16T11:59:00Z')
            entry.update(changes)
            return entry

        cases = {
            'newer pending': lambda s: s['workflow_runs'].insert(0, newer(status='in_progress', conclusion=None)),
            'newer failure': lambda s: s['workflow_runs'].insert(0, newer(conclusion='failure')),
            'newer cancelled': lambda s: s['workflow_runs'].insert(0, newer(conclusion='cancelled')),
            'newer success not selected': lambda s: s['workflow_runs'].insert(0, newer()),
            'newer appended at end': lambda s: s['workflow_runs'].append(newer()),
            'selected run absent': lambda s: s['workflow_runs'].pop(0),
            'listing entry differs': lambda s: s['workflow_runs'][0].update(conclusion='failure'),
            'duplicate listing ids': lambda s: s['workflow_runs'].append(copy.deepcopy(s['workflow_runs'][0])),
            'empty listing': lambda s: s.update(workflow_runs=[]),
            'incomplete listing': lambda s: s.update(complete=False),
            'string complete': lambda s: s.update(complete='true'),
            'older pull_request run': lambda s: s['workflow_runs'][1].update(event='pull_request'),
            'older feature-branch dispatch': lambda s: s['workflow_runs'][1].update(event='workflow_dispatch', head_branch='feature'),
            'older foreign workflow': lambda s: s['workflow_runs'][1].update(workflow_id=POLICY['workflow_id'] + 1),
            'older foreign path': lambda s: s['workflow_runs'][1].update(path='.github/workflows/other.yml'),
            'malformed listing entry': lambda s: s['workflow_runs'].append({'id': 1}),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                case = Case()
                mutate(case.before)
                with self.assertRaises(aj.ProvenanceError):
                    case.evaluate()
        # An older successful run alongside the selected newest run is fine.
        self.assertTrue(Case().evaluate()['diagnostic_valid'])

    def test_artifact_rules(self):
        observed = FIXTURE['snapshot']['observed_at']
        cases = {
            'missing': lambda s, m: s['artifacts'].remove(m),
            'deleted (empty listing)': lambda s, m: s.update(artifacts=[]),
            'duplicate name': lambda s, m: s['artifacts'].append({**copy.deepcopy(m), 'id': m['id'] + 7}),
            'duplicate id': lambda s, m: s['artifacts'].append(copy.deepcopy(m)),
            'expired flag': lambda s, m: m.update(expired=True),
            'expires at observation': lambda s, m: m.update(expires_at=observed),
            'expired before observation': lambda s, m: m.update(expires_at='2026-10-16T11:59:59Z'),
            'null digest': lambda s, m: m.update(digest=None),
            'md5 digest': lambda s, m: m.update(digest='md5:' + 'a' * 32),
            'uppercase digest': lambda s, m: m.update(digest='sha256:' + 'A' * 64),
            'foreign run link': lambda s, m: m['workflow_run'].update(id=m['workflow_run']['id'] + 1),
            'foreign repository link': lambda s, m: m['workflow_run'].update(repository_id=1),
            'fork head repository link': lambda s, m: m['workflow_run'].update(head_repository_id=1),
            'link head sha moved': lambda s, m: m['workflow_run'].update(head_sha=OTHER_SHA),
            'link branch': lambda s, m: m['workflow_run'].update(head_branch='feature'),
            'size over bound': lambda s, m: m.update(size_in_bytes=POLICY['bounds']['max_archive_bytes'] + 1),
            'zero size': lambda s, m: m.update(size_in_bytes=0),
            'bool id': lambda s, m: m.update(id=True),
            'unknown field': lambda s, m: m.update(node_id='x'),
            'missing field': lambda s, m: m.pop('expires_at'),
            'timestamps reversed': lambda s, m: m.update(created_at='2026-10-16T11:55:00Z'),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                case = Case()
                mutate(case.before, case.artifact())
                with self.assertRaises(aj.ProvenanceError):
                    case.evaluate()

    def test_unavailable_evidence_blocks_without_fallback(self):
        for reason in ('http-403', 'http-404', 'http-410', 'http-429 rate limited', 'transport timeout',
                       'unknown', '', 'x' * 500, None, 7):
            with self.subTest(reason=reason):
                case = Case()
                for position in ('before', 'after'):
                    unavailable = {'unavailable': reason}
                    with self.assertRaises(aj.ProvenanceError) as caught:
                        if position == 'before':
                            aj.evaluate_offline(case.policy, unavailable, case.before, case.archive, case.candidate, case.previous)
                        else:
                            aj.evaluate_offline(case.policy, case.before, unavailable, case.archive, case.candidate, case.previous)
                    self.assertIn('evidence unavailable', str(caught.exception))
                    if isinstance(reason, str) and reason and len(reason) < 80:
                        self.assertIn(reason, str(caught.exception))
        for missing in (None, {}, [], 'snapshot'):
            with self.subTest(missing=missing), self.assertRaises(aj.ProvenanceError):
                aj.validate_snapshot(missing, POLICY)
        for evidence in (None, {'unavailable': 'http-410'}):
            status = aj.journal_status(evidence, FIXTURE['candidate'])
            self.assertEqual(status['semantic_state'], 'evidence-unavailable')
            self.assertNotEqual(status['semantic_state'], 'not-dispatched')
            self.assertTrue(status['operator_reconciliation_required'])

    def test_before_after_movement_blocks(self):
        for name, mutate in {
            'artifact updated': lambda a: a['artifacts'][0].update(updated_at='2026-10-16T12:00:01Z'),
            'artifact digest changed': lambda a: a['artifacts'][0].update(digest='sha256:' + 'e' * 64),
            'newer run appeared': lambda a: a['workflow_runs'].insert(0, {**copy.deepcopy(a['run']), 'id': a['run']['id'] + 1}),
            'listing reordered': lambda a: a['workflow_runs'].reverse(),
            'default branch moved': lambda a: a.update(default_branch_head=OTHER_SHA),
            'artifact disappeared': lambda a: a['artifacts'].pop(0),
            'observed earlier than before': lambda a: a.update(observed_at='2026-10-16T11:59:00Z'),
        }.items():
            with self.subTest(name=name):
                case = Case()
                after = copy.deepcopy(case.before)
                after['observed_at'] = case.after_observed_at
                mutate(after)
                with self.assertRaises(aj.ProvenanceError):
                    case.evaluate(sync=False, after=after)
        # Identical bodies at two observation times are a stable snapshot pair.
        case = Case()
        self.assertTrue(case.evaluate(sync=False, after={**copy.deepcopy(case.before), 'observed_at': case.before['observed_at']})['diagnostic_valid'])

    def test_default_branch_and_policy_movement(self):
        case = Case()
        case.before['default_branch_head'] = OTHER_SHA
        with self.assertRaises(aj.ProvenanceError):
            case.evaluate()
        case = Case()  # policy advanced after the run: run.head_sha is stale
        case.policy['policy_commit'] = OTHER_SHA
        case.policy['source_closure']['commit'] = OTHER_SHA
        with self.assertRaises(aj.ProvenanceError):
            case.evaluate()
        case = Case()  # snapshot and run agree but the trusted policy pins a different workflow
        case.policy['workflow_id'] += 1
        with self.assertRaises(aj.ProvenanceError):
            case.evaluate()


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.doc = copy.deepcopy(FIXTURE['journal_document'])
        self.X = FIXTURE['candidate']
        self.Z = FIXTURE['other_candidates']['unrecorded']['candidate']
        self.A = self.doc['records'][0]['operation_id']

    def test_unknown_outcome_and_lost_evidence_never_authorize_redispatch(self):
        status = aj.journal_status(self.doc, self.X)
        self.assertEqual(status['semantic_state'], 'unknown-outcome')
        self.assertTrue(status['operator_reconciliation_required'])
        self.assertFalse(status['in_flight'])
        for previous in ({'kind': 'unavailable', 'reason': 'artifact expired'}, {'kind': 'unavailable'},
                         {'kind': 'unavailable', 'reason': 'http-410'}, {'kind': 'lost'}, {'kind': 'genesis', 'force': True},
                         {}, None, 'genesis'):
            with self.subTest(previous=previous), self.assertRaises(aj.JournalError):
                aj.reconcile(previous, self.doc)
        # A fresh genesis after real history is a manufactured reset.
        genesis = copy.deepcopy(self.doc)
        genesis['records'] = []
        with self.assertRaises(aj.JournalError):
            aj.reconcile({'kind': 'journal', 'document': self.doc}, genesis)
        case = Case()
        case.previous = {'kind': 'unavailable', 'reason': 'artifact deleted'}
        with self.assertRaises(aj.JournalError):
            case.evaluate()

    def test_in_flight_states_are_not_redispatchable(self):
        for state, expected in (('reserved', 'reserved'), ('attempted', 'attempted')):
            doc = copy.deepcopy(self.doc)
            doc['records'] = [record(1, self.A, self.X, 'reserved')]
            if state == 'attempted':
                doc['records'].append(record(2, self.A, self.X, 'attempted'))
            status = aj.journal_status(doc, self.X)
            self.assertEqual(status['semantic_state'], expected)
            self.assertTrue(status['in_flight'])
            self.assertNotEqual(status['semantic_state'], 'not-dispatched')

    def test_dropped_or_rewritten_records_are_rejected(self):
        prev = self.doc
        chained = {'kind': 'journal', 'document': prev}
        self.assertEqual(aj.reconcile(chained, copy.deepcopy(prev)), 'unchanged')
        extended = successor(prev, record(7, 'ef' * 16, self.Z, 'reserved'))
        self.assertEqual(aj.reconcile(chained, extended), 'extended')
        bad = {}
        dropped = successor(prev)
        del dropped['records'][2]
        for i, r in enumerate(dropped['records'], start=1):
            r['sequence'] = i
        bad['unknown record dropped and resequenced'] = dropped
        rewritten = successor(prev)
        rewritten['records'][2]['state'] = 'rejected'
        bad['unknown rewritten to rejected'] = rewritten
        erased = successor(prev)
        erased['records'] = erased['records'][3:]
        bad['unresolved operation erased'] = erased
        bad['sequence skipped'] = successor(prev, journal_sequence=prev['journal_sequence'] + 2)
        # Same sequence number and null predecessor as the previous journal, but with a new
        # record appended: content changed without advancing the chain. (An identical
        # re-observation of the previous document is the permitted 'unchanged' case above.)
        bad['same sequence changed'] = successor(prev, record(7, 'ef' * 16, self.Z, 'reserved'),
                                                 journal_sequence=prev['journal_sequence'], predecessor_digest=None)
        self.assertNotEqual(bad['same sequence changed'], prev)
        bad['wrong predecessor digest'] = successor(prev, predecessor_digest='0' * 64)
        bad['identity changed'] = successor(prev, repository='example-org/fork')
        bad['identity id changed'] = successor(prev, repository_id=prev['repository_id'] + 1)
        bad['terminal record rewritten'] = successor(prev)
        bad['terminal record rewritten']['records'][5]['state'] = 'rejected'
        bad['record candidate rewritten'] = successor(prev)
        bad['record candidate rewritten']['records'][0]['candidate'] = self.Z
        for name, current in bad.items():
            with self.subTest(name=name), self.assertRaises(aj.JournalError):
                aj.reconcile(chained, current)

    def test_policy_and_candidate_movement_do_not_reset_the_chain(self):
        older_policy = copy.deepcopy(self.doc)
        older_policy['policy_commit'] = OTHER_SHA
        older_policy['producer']['runtime_sha'] = OTHER_SHA
        current = successor(older_policy, policy_commit=self.doc['policy_commit'],
                            producer=self.doc['producer'])
        self.assertEqual(aj.reconcile({'kind': 'journal', 'document': older_policy}, current), 'extended')
        with self.assertRaises(aj.JournalError):
            aj.reconcile({'kind': 'journal', 'document': older_policy}, self.doc)  # fresh genesis after policy move
        # Candidate movement: a new head is a different candidate; the old unknown stays.
        moved = {**self.X, 'head': 'c9' * 20, 'merge': 'da' * 20, 'tree': 'eb' * 20}
        current = successor(self.doc, record(7, 'ef' * 16, moved, 'reserved'))
        self.assertEqual(aj.journal_status(current, moved)['semantic_state'], 'reserved')
        self.assertEqual(aj.journal_status(current, self.X)['semantic_state'], 'unknown-outcome')
        # End to end: a chained successor validates and still carries no authority.
        case = Case()
        case.previous = {'kind': 'journal', 'document': copy.deepcopy(self.doc)}
        case.set_document(successor(self.doc, record(7, 'ef' * 16, self.Z, 'reserved')))
        result = case.evaluate()
        self.assertEqual(result['chain'], 'extended')
        self.assertIs(result['dispatch_allowed'], False)
        self.assertIs(result['admitted'], False)

    def test_moved_head_does_not_bypass_pr_level_unresolved_hold(self):
        X, A, B = self.X, self.A, 'ef' * 16
        moved = {
            'head moved': {**X, 'head': 'c9' * 20, 'merge': 'da' * 20, 'tree': 'eb' * 20},
            'base moved': {**X, 'base': 'c9' * 20, 'merge': 'da' * 20, 'tree': 'eb' * 20},
            'merge moved': {**X, 'merge': 'da' * 20},
            'tree moved': {**X, 'tree': 'eb' * 20},
        }

        def journal(*states, candidate=X):
            doc = copy.deepcopy(self.doc)
            doc['records'] = [record(i + 1, A, candidate, s) for i, s in enumerate(states)]
            aj.validate_records(doc['records'], X['repo'], 10, 300000001)
            return doc

        unresolved = {'reserved': journal('reserved'), 'attempted': journal('reserved', 'attempted'),
                      'unknown': journal('reserved', 'attempted', 'unknown')}
        for old_state, doc in unresolved.items():
            for name, candidate in moved.items():
                with self.subTest(old_state=old_state, moved=name):
                    status = aj.journal_status(doc, candidate)
                    self.assertEqual(status['semantic_state'], 'pr-unresolved')
                    self.assertNotEqual(status['semantic_state'], 'not-dispatched')
                    self.assertEqual(status['candidate_state'], 'not-dispatched')
                    self.assertTrue(status['operator_reconciliation_required'])
                    self.assertFalse(status['in_flight'])
                    self.assertEqual(status['operations'], [])
                    self.assertEqual(status['pr_unresolved_operations'], [A])
            # Another PR in the same repository is isolated from this PR's ledger.
            other = aj.journal_status(doc, self.Z)
            self.assertEqual(other['semantic_state'], 'not-dispatched')
            self.assertFalse(other['operator_reconciliation_required'])
            self.assertEqual(other['pr_unresolved_operations'], [])
        # Resolved terminal operations do not falsely hold a moved head.
        for terminal in ('confirmed', 'rejected'):
            doc = journal('reserved', 'attempted', terminal)
            for name, candidate in moved.items():
                with self.subTest(terminal=terminal, moved=name):
                    status = aj.journal_status(doc, candidate)
                    self.assertEqual(status['semantic_state'], 'not-dispatched')
                    self.assertFalse(status['operator_reconciliation_required'])
                    self.assertEqual(status['pr_unresolved_operations'], [])
        # Per-candidate detail is preserved: the moved head's own reservation reads
        # 'reserved' while the old head's unknown outcome still forces PR reconciliation.
        head = moved['head moved']
        current = successor(self.doc, record(7, B, head, 'reserved'))
        status = aj.journal_status(current, head)
        self.assertEqual(status['semantic_state'], 'reserved')
        self.assertEqual(status['candidate_state'], 'reserved')
        self.assertTrue(status['in_flight'])
        self.assertTrue(status['operator_reconciliation_required'])
        self.assertEqual(status['pr_unresolved_operations'], sorted([A, B]))
        # A confirmed HTTP rejection for the moved head is still held by the old unknown.
        rejected = successor(self.doc, record(7, B, head, 'reserved'), record(8, B, head, 'attempted'),
                             record(9, B, head, 'rejected'))
        aj.validate_records(rejected['records'], X['repo'], 10, 300000001)
        status = aj.journal_status(rejected, head)
        self.assertEqual(status['candidate_state'], 'rejected')
        self.assertEqual(status['semantic_state'], 'pr-unresolved')
        self.assertTrue(status['operator_reconciliation_required'])
        self.assertEqual(status['pr_unresolved_operations'], [A])
        # Neither policy movement nor candidate movement resets the unresolved ledger.
        older_policy = copy.deepcopy(self.doc)
        older_policy['policy_commit'] = OTHER_SHA
        older_policy['producer']['runtime_sha'] = OTHER_SHA
        after_policy = successor(older_policy, record(7, B, head, 'reserved'),
                                 policy_commit=self.doc['policy_commit'], producer=self.doc['producer'])
        self.assertEqual(aj.reconcile({'kind': 'journal', 'document': older_policy}, after_policy), 'extended')
        self.assertEqual(aj.journal_status(after_policy, head)['pr_unresolved_operations'], sorted([A, B]))
        self.assertEqual(aj.journal_status(after_policy, X)['semantic_state'], 'unknown-outcome')
        # End to end: the moved head is explicitly held and carries no authority.
        case = Case()
        case.candidate = head
        result = case.evaluate()
        self.assertEqual(result['semantic_state'], 'pr-unresolved')
        self.assertEqual(result['candidate_state'], 'not-dispatched')
        self.assertTrue(result['operator_reconciliation_required'])
        self.assertEqual(result['pr_unresolved_operations'], [A])
        self.assertIs(result['dispatch_allowed'], False)
        self.assertIs(result['admitted'], False)

    def test_resolving_an_open_operation_appends_rather_than_rewrites(self):
        prev = copy.deepcopy(self.doc)
        prev['records'] = [record(1, self.A, self.X, 'reserved'), record(2, self.A, self.X, 'attempted')]
        self.assertEqual(aj.journal_status(prev, self.X)['semantic_state'], 'attempted')
        for terminal in ('confirmed', 'rejected', 'unknown'):
            current = successor(prev, record(3, self.A, self.X, terminal))
            aj.validate_records(current['records'], self.X['repo'], 10, 300000001)
            self.assertEqual(aj.reconcile({'kind': 'journal', 'document': prev}, current), 'extended')
        after_unknown = successor(prev, record(3, self.A, self.X, 'unknown'), record(4, self.A, self.X, 'confirmed'))
        with self.assertRaises(aj.JournalError):
            aj.validate_records(after_unknown['records'], self.X['repo'], 10, 300000001)


class DisabledEntryTests(unittest.TestCase):
    KNOBS = {'CANDIDATE_ARTIFACT_JOURNAL_ENABLED': '1', 'ARTIFACT_JOURNAL_ACTIVATE': 'true',
             'ENABLE_SIDE_EFFECTS': 'yes', 'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_REF': 'refs/heads/main',
             'GITHUB_RUN_ATTEMPT': '1', 'GH_TOKEN': 'synthetic-not-a-token'}

    def test_disabled_entry_rejects_every_payload_and_activation_knob(self):
        case = Case()
        side_effect = mock.Mock()
        valid = {'policy': case.policy, 'before': case.before, 'after': copy.deepcopy(case.before),
                 'archive': case.archive, 'candidate': case.candidate, 'previous': case.previous}
        for payload in (None, {}, {'enable': True}, {'dispatch_allowed': True, 'admitted': True},
                        {'activation': {'approved': True}}, valid, case.evaluate()):
            with self.subTest(payload=type(payload).__name__):
                result = aj.disabled_entry(payload, environ=self.KNOBS, side_effect=side_effect,
                                           activate=True, dispatch=side_effect, post=side_effect)
                self.assertEqual(result['admitted'], 'false')
                self.assertEqual(result['dispatch_allowed'], 'false')
                self.assertEqual(result['side_effects'], 'rejected')
                self.assertEqual(result['action'], 'artifact-journal-disabled')
        side_effect.assert_not_called()
        self.assertIsNot(aj.disabled_entry(), aj.DISABLED)  # a copy; callers cannot mutate the constant
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.KNOBS), contextlib.redirect_stdout(output):
            code = aj.main(['--activate', '--dispatch'], self.KNOBS)
        self.assertEqual(code, 1)
        self.assertIn('artifact-journal-disabled', output.getvalue())
        self.assertNotIn('synthetic-not-a-token', output.getvalue())

    ALLOWED_IMPORTS = {'__future__', 'hashlib', 'io', 'json', 're', 'zipfile', 'zlib', 'datetime'}
    FORBIDDEN_NAMES = {'open', 'exec', 'eval', 'compile', '__import__'}
    FORBIDDEN_ATTRIBUTES = {'environ', 'getenv', 'getenvb', 'extractall', 'extract', 'system', 'popen',
                            'Popen', 'urlopen'}

    def test_module_exposes_no_activation_switch_transport_or_extraction(self):
        names = [n for n in dir(aj) if any(w in n.lower() for w in ('enable', 'activat', 'token', 'secret'))]
        self.assertEqual(names, [])
        # Runtime namespace: no transport, process, environment or filesystem module is bound.
        for module in ('os', 'sys', 'subprocess', 'socket', 'urllib', 'http', 'ssl', 'tempfile', 'shutil', 'pathlib'):
            self.assertFalse(hasattr(aj, module), module)
        # Structural check over code only (docstrings and comments may say "environment").
        tree = ast.parse(Path(aj.__file__).read_text(encoding='utf-8'))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or '').split('.')[0])
            elif isinstance(node, ast.Name):
                self.assertNotIn(node.id, self.FORBIDDEN_NAMES, node.id)
            elif isinstance(node, ast.Attribute):
                # ZipFile.open on in-memory bytes is the only read; builtin open is a forbidden Name above.
                self.assertNotIn(node.attr, self.FORBIDDEN_ATTRIBUTES, node.attr)
        self.assertLessEqual(imported, self.ALLOWED_IMPORTS, imported - self.ALLOWED_IMPORTS)

    def test_stage_authority_fields_cannot_be_overridden(self):
        malicious = {'stage': 'production-dispatch', 'admitted': True, 'dispatch_allowed': True,
                     'side_effects': 'allowed', 'diagnostic_valid': True}
        result = aj.stage(**malicious)
        self.assertIs(result['admitted'], False)
        self.assertIs(result['dispatch_allowed'], False)
        self.assertEqual(result['side_effects'], 'rejected')
        self.assertEqual(result['stage'], aj.STAGE)
        self.assertEqual(aj.stage(stage='x')['stage'], aj.STAGE)
        self.assertEqual(aj.stage(**{'stage': 'x', 'admitted': 'true', 'dispatch_allowed': 'true'}),
                         {'stage': aj.STAGE, 'admitted': False, 'dispatch_allowed': False, 'side_effects': 'rejected'})
        for text in (json.dumps(Case().evaluate()), json.dumps(aj.disabled_entry())):
            self.assertNotIn('"dispatch_allowed": true', text)
            self.assertNotIn('"admitted": true', text)
            self.assertNotIn('"dispatch_allowed": "true"', text)
            self.assertNotIn('"admitted": "true"', text)

    def test_existing_gate_is_not_wired_to_this_module(self):
        for path in ('scripts/ci/candidate_gate.py', 'scripts/ci/merge_candidate.py', 'scripts/ci/codex_gate.py',
                     'scripts/ci/claude_fixer_receipts.py'):
            self.assertNotIn('candidate_artifact_journal', (ROOT / path).read_text(encoding='utf-8'), path)
        for workflow in (ROOT / '.github/workflows').glob('*.yml'):
            self.assertNotIn('candidate_artifact_journal', workflow.read_text(encoding='utf-8'), workflow.name)


if __name__ == '__main__':
    unittest.main()
