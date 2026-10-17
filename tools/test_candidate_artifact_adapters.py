"""Offline tests for the disabled artifact-journal producer/consumer adapters (CSL-259).

Everything here is synthetic: the transport is an in-memory fake over the neutral
fixture. A passing suite proves that the adapters compose the validator correctly and
fail closed; it observes no GitHub behaviour and approves no trust or activation.

Run: python -X utf8 -B tools/test_candidate_artifact_adapters.py
"""
from __future__ import annotations

import ast
import contextlib
import copy
import importlib
import io
import json
import os
import sys
import unittest
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
aj = importlib.import_module('candidate_artifact_journal')
ad = importlib.import_module('candidate_artifact_adapters')

FIXTURE = json.loads((ROOT / 'tools/fixtures/candidate-artifact-journal-v1.json').read_text(encoding='utf-8'))
POLICY = FIXTURE['trusted_policy']
DOCUMENT = FIXTURE['journal_document']
CONTEXT = DOCUMENT['producer']
EVENTS = [{k: r[k] for k in ad.EVENT_KEYS} for r in DOCUMENT['records']]
CANDIDATE = FIXTURE['candidate']
BEFORE_AT = FIXTURE['snapshot']['observed_at']
AFTER_AT = FIXTURE['snapshot_after_observed_at']
OTHER_SHA = 'b' * 40
MOVED_HEAD = {**CANDIDATE, 'head': 'c9' * 20, 'merge': 'da' * 20, 'tree': 'eb' * 20}
SIDE_EFFECTS = ('post_comment', 'list_issue_comments', 'upload_artifact', 'dispatch_workflow', 'add_label',
                'delete_artifact')


def noisy(obj, **extra):
    """A REST-shaped object: the projected fields plus fields the validator must never see."""
    out = copy.deepcopy(obj)
    out.update({'node_id': 'MDEx', 'html_url': 'https://example.invalid/x', 'trusted': True}, **extra)
    for key in ('repository', 'head_repository', 'actor', 'triggering_actor', 'workflow_run'):
        if isinstance(out.get(key), dict):
            out[key] = {**out[key], 'node_id': 'MDEx', 'url': 'https://example.invalid/y'}
    return out


class Transport:
    """Read-only fake over a synthetic REST state; records every call in order.

    ``unavailable[method] = reason`` fails calls to that method once the per-method call
    count exceeds ``unavailable_after.get(method, 0)``. ``on_download`` mutates the
    state after the download so the after-snapshot can differ from the before-snapshot.
    """

    def __init__(self, state, archive, page_size=100):
        self.state, self.archive, self.page_size = state, archive, page_size
        self.calls, self.counts = [], {}
        self.unavailable, self.unavailable_after, self.on_download = {}, {}, None

    def _reply(self, method, value):
        self.counts[method] = self.counts.get(method, 0) + 1
        if method in self.unavailable and self.counts[method] > self.unavailable_after.get(method, 0):
            return {'unavailable': self.unavailable[method]}
        return {'ok': value}

    def _page(self, method, items, page):
        start = (page - 1) * self.page_size
        return self._reply(method, {'items': items[start:start + self.page_size],
                                    'has_next': start + self.page_size < len(items)})

    def get_ref(self, repository, branch):
        self.calls.append(('get_ref', repository, branch))
        return self._reply('get_ref', self.state['ref'])

    def list_workflow_runs(self, repository, workflow_id, page):
        self.calls.append(('list_workflow_runs', repository, workflow_id, page))
        return self._page('list_workflow_runs', self.state['runs'], page)

    def get_workflow_run(self, repository, run_id):
        self.calls.append(('get_workflow_run', repository, run_id))
        return self._reply('get_workflow_run', self.state['run_by_id'].get(run_id, {}))

    def list_artifacts(self, repository, run_id, page):
        self.calls.append(('list_artifacts', repository, run_id, page))
        return self._page('list_artifacts', self.state['artifacts'].get(run_id, []), page)

    def download_artifact(self, repository, artifact_id, max_bytes):
        # The cap is recorded, type-checked and otherwise ignored on purpose: this fake
        # is a passthrough so the adapter's own oversize rejection is observable. A real
        # transport must stop streaming at ``max_bytes`` before allocating the body.
        self.calls.append(('download_artifact', repository, artifact_id, max_bytes))
        if not (type(max_bytes) is int and max_bytes > 0):
            return {'unavailable': 'download cap missing'}
        reply = self._reply('download_artifact', self.archive)
        if self.on_download is not None:
            self.on_download(self.state)
        return reply

    def methods(self):
        return [call[0] for call in self.calls]


class TemptingTransport(Transport):
    """Also offers every write/fallback method a careless consumer might reach for."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in SIDE_EFFECTS:
            setattr(self, name, mock.Mock(name=name))

    def assert_read_only(self, test):
        for name in SIDE_EFFECTS:
            getattr(self, name).assert_not_called()
        test.assertLessEqual(set(self.methods()), set(ad.READ_METHODS))


def rest_state(snapshot, artifact_meta):
    """Synthetic REST state from the fixture snapshot with the real archive metadata."""
    snapshot = copy.deepcopy(snapshot)
    for meta in snapshot['artifacts']:
        if meta['name'] == POLICY['artifact_name']:
            meta.update(artifact_meta)
    run_id = snapshot['run']['id']
    return {'ref': snapshot['default_branch_head'],
            'runs': [noisy(r) for r in snapshot['workflow_runs']],
            'run_by_id': {r['id']: noisy(r) for r in snapshot['workflow_runs']},
            'artifacts': {run_id: [noisy(m) for m in snapshot['artifacts']]}}


class Scenario:
    """Fixture-derived producer output served through a fake transport."""

    def __init__(self, page_size=100, tempting=False, previous=None, events=None, context=None):
        self.policy = copy.deepcopy(POLICY)
        self.previous = copy.deepcopy(previous if previous is not None else FIXTURE['previous_evidence'])
        self.produced = ad.produce(self.policy, copy.deepcopy(context or CONTEXT), self.previous,
                                   copy.deepcopy(events if events is not None else EVENTS))
        self.document, self.archive = self.produced['document'], self.produced['archive']
        self.state = rest_state(FIXTURE['snapshot'], self.produced['expected_artifact'])
        cls = TemptingTransport if tempting else Transport
        self.transport = cls(self.state, self.archive, page_size=page_size)
        self.candidate = copy.deepcopy(CANDIDATE)

    def journal_artifact(self):
        run_id = FIXTURE['snapshot']['run']['id']
        return next(m for m in self.state['artifacts'][run_id] if m['name'] == POLICY['artifact_name'])

    def consume(self, before=BEFORE_AT, after=AFTER_AT):
        return ad.consume(self.transport, self.policy, self.candidate, self.previous, before, after)


FULL_SEQUENCE = ['get_ref', 'list_workflow_runs', 'get_workflow_run', 'list_artifacts', 'download_artifact',
                 'get_ref', 'list_workflow_runs', 'get_workflow_run', 'list_artifacts']


class ProducerTests(unittest.TestCase):
    def test_assembly_reproduces_the_neutral_fixture_document_deterministically(self):
        first = ad.produce(POLICY, CONTEXT, FIXTURE['previous_evidence'], EVENTS)
        second = ad.produce(copy.deepcopy(POLICY), copy.deepcopy(CONTEXT), {'kind': 'genesis'}, copy.deepcopy(EVENTS))
        self.assertEqual(first['document'], DOCUMENT)
        self.assertEqual(first['archive'], second['archive'])
        self.assertEqual(first['journal_digest'], aj.canonical_digest(DOCUMENT))
        self.assertIs(first['uploaded'], False)
        self.assertEqual(first['stage'], ad.STAGE)
        # Produced bytes: one stored regular-file entry, fixed timestamp, no comment,
        # accepted by the validator's layer 1 under the metadata the upload must later show.
        with zipfile.ZipFile(io.BytesIO(first['archive'])) as container:
            (info,) = container.infolist()
            self.assertEqual((info.filename, info.compress_type, info.date_time, container.comment),
                             (POLICY['journal_entry'], zipfile.ZIP_STORED, ad.ZIP_EPOCH, b''))
            self.assertEqual(info.external_attr >> 16, 0o100644)
        meta = first['expected_artifact']
        self.assertEqual(meta['size_in_bytes'], len(first['archive']))
        self.assertEqual(aj.verify_archive(first['archive'], meta, POLICY), DOCUMENT)
        # Inputs are not mutated and the result does not alias them.
        events = copy.deepcopy(EVENTS)
        result = ad.produce(POLICY, CONTEXT, {'kind': 'genesis'}, events)
        self.assertEqual(events, EVENTS)
        result['document']['records'][0]['candidate']['pr'] = 1
        self.assertEqual(events, EVENTS)

    def test_extension_preserves_previous_records_and_unresolved_holds(self):
        previous = {'kind': 'journal', 'document': copy.deepcopy(DOCUMENT)}
        appended = {'operation_id': 'ef' * 16, 'candidate': MOVED_HEAD, 'operation': 'label-post', 'state': 'reserved'}
        context = {**CONTEXT, 'run_id': CONTEXT['run_id'] + 5}
        document = ad.build_journal_document(POLICY, context, previous, [appended])
        self.assertEqual(document['journal_sequence'], 2)
        self.assertEqual(document['predecessor_digest'], aj.canonical_digest(DOCUMENT))
        self.assertEqual(document['records'][:6], DOCUMENT['records'])
        self.assertEqual(document['records'][6], {'sequence': 7, 'run_id': context['run_id'], **appended})
        self.assertEqual(aj.reconcile(previous, document), 'extended')
        # The old head's unknown outcome survives the moved head's new reservation.
        self.assertEqual(aj.journal_status(document, CANDIDATE)['semantic_state'], 'unknown-outcome')
        moved = aj.journal_status(document, MOVED_HEAD)
        self.assertEqual(moved['semantic_state'], 'reserved')
        self.assertTrue(moved['operator_reconciliation_required'])
        self.assertEqual(moved['pr_unresolved_operations'], sorted([DOCUMENT['records'][0]['operation_id'], 'ef' * 16]))
        # An extension with no events is a legal empty chain step, never a rewrite.
        self.assertEqual(aj.reconcile(previous, ad.build_journal_document(POLICY, context, previous, [])), 'extended')
        # Resolving an open operation appends; the producer cannot be asked to rewrite.
        resolved = ad.build_journal_document(POLICY, context, previous, [
            {'operation_id': 'ef' * 16, 'candidate': MOVED_HEAD, 'operation': 'label-post', 'state': 'reserved'},
            {'operation_id': 'ef' * 16, 'candidate': MOVED_HEAD, 'operation': 'label-post', 'state': 'attempted'},
            {'operation_id': 'ef' * 16, 'candidate': MOVED_HEAD, 'operation': 'label-post', 'state': 'confirmed'}])
        self.assertEqual(len(resolved['records']), 9)
        self.assertEqual(aj.journal_status(resolved, MOVED_HEAD)['candidate_state'], 'confirmed')
        self.assertEqual(aj.journal_status(resolved, MOVED_HEAD)['semantic_state'], 'pr-unresolved')

    def test_producer_context_must_bind_the_policy(self):
        qualified = aj.workflow_ref(POLICY)
        bad = {
            'rerun attempt': {'run_attempt': 2}, 'bool attempt': {'run_attempt': True}, 'bool run id': {'run_id': True},
            'zero run id': {'run_id': 0}, 'candidate event': {'event': 'pull_request'}, 'push event': {'event': 'push'},
            'runtime sha is candidate merge': {'runtime_sha': CANDIDATE['merge']}, 'runtime sha moved': {'runtime_sha': OTHER_SHA},
            'bare workflow ref': {'workflow_ref': POLICY['workflow_path']},
            'foreign workflow ref': {'workflow_ref': 'example-org/fork/' + POLICY['workflow_path'] + '@refs/heads/main'},
            'feature ref': {'workflow_ref': qualified.replace('refs/heads/main', 'refs/heads/feature')},
            'sha ref': {'workflow_ref': qualified.replace('refs/heads/main', POLICY['policy_commit'])},
            'trusted flag': {'trusted': True}, 'attestation': {'attestation': 'verified'},
            'mixed key types': {1: 'x'}, 'none key': {None: 'x'},
        }
        for name, change in bad.items():
            with self.subTest(name=name), self.assertRaises(aj.JournalError):
                ad.build_journal_document(POLICY, {**CONTEXT, **change}, {'kind': 'genesis'}, EVENTS)
        with self.assertRaises(aj.JournalError):
            ad.build_journal_document(POLICY, {k: v for k, v in CONTEXT.items() if k != 'workflow_ref'}, {'kind': 'genesis'}, EVENTS)
        # Event not in the (narrowed) policy, and a policy the validator rejects.
        with self.assertRaises(aj.JournalError):
            ad.build_journal_document({**POLICY, 'producer_events': ['workflow_dispatch']}, CONTEXT, {'kind': 'genesis'}, EVENTS)
        with self.assertRaises(aj.ProvenanceError):
            ad.build_journal_document({**POLICY, 'source_closure': {**POLICY['source_closure'], 'commit': OTHER_SHA}},
                                      CONTEXT, {'kind': 'genesis'}, EVENTS)

    def test_events_and_previous_evidence_rules(self):
        A = DOCUMENT['records'][0]['operation_id']
        reserved = {'operation_id': 'ef' * 16, 'candidate': CANDIDATE, 'operation': 'label-post', 'state': 'reserved'}
        bad_events = {
            'first state attempted': [{**reserved, 'state': 'attempted'}],
            'skip attempted': [reserved, {**reserved, 'state': 'confirmed'}],
            'after terminal': [reserved, {**reserved, 'state': 'attempted'}, {**reserved, 'state': 'unknown'}, {**reserved, 'state': 'confirmed'}],
            'rebound candidate': [reserved, {**reserved, 'candidate': MOVED_HEAD, 'state': 'attempted'}],
            'rebound operation': [reserved, {**reserved, 'operation': 'comment-post', 'state': 'attempted'}],
            'foreign repo candidate': [{**reserved, 'candidate': {**CANDIDATE, 'repo': 'example-org/fork'}}],
            'candidate trust flag': [{**reserved, 'candidate': {**CANDIDATE, 'trusted': True}}],
            'short sha': [{**reserved, 'candidate': {**CANDIDATE, 'head': 'c1' * 19}}],
            'event extra field': [{**reserved, 'note': 'x'}], 'event with sequence': [{**reserved, 'sequence': 1}],
            'event with run id': [{**reserved, 'run_id': 1}], 'unknown operation': [{**reserved, 'operation': 'merge'}],
            'unknown state': [{**reserved, 'state': 'done'}], 'not a dict': ['reserved'],
            'continue another run operation': [{**reserved, 'operation_id': A, 'state': 'reserved'}],
            'event mixed key types': [{**reserved, 1: 'x'}], 'event tuple key': [{**reserved, ('state',): 'x'}],
        }
        for name, events in bad_events.items():
            with self.subTest(name=name), self.assertRaises(aj.JournalError):
                ad.build_journal_document(POLICY, CONTEXT, {'kind': 'genesis'}, EVENTS + events)
        with self.assertRaises(aj.JournalError):
            ad.build_journal_document(POLICY, CONTEXT, {'kind': 'genesis'}, tuple(EVENTS))
        for previous in ({'kind': 'unavailable', 'reason': 'artifact expired'}, {'kind': 'unavailable'}, {'kind': 'lost'},
                         {'kind': 'genesis', 'force': True}, {'kind': 'journal'}, {'kind': 'journal', 'document': {}},
                         {'kind': 'journal', 'document': {**DOCUMENT, 'journal_sequence': 0}},
                         {'kind': 'journal', 'document': DOCUMENT, 'extra': 1}, {}, None, 'genesis', ['genesis']):
            with self.subTest(previous=previous), self.assertRaises(aj.JournalError):
                ad.build_journal_document(POLICY, CONTEXT, previous, [])
        # A previous journal from another repository cannot be extended under this policy.
        foreign = {'kind': 'journal', 'document': {**copy.deepcopy(DOCUMENT), 'repository_id': POLICY['repository_id'] + 1}}
        with self.assertRaises(aj.JournalError):
            ad.build_journal_document(POLICY, CONTEXT, foreign, [])

    def test_bounds_are_enforced_on_produced_bytes_and_records(self):
        tight = copy.deepcopy(POLICY)
        tight['bounds']['max_records'] = len(EVENTS) - 1
        with self.assertRaises(aj.JournalError):
            ad.produce(tight, CONTEXT, {'kind': 'genesis'}, EVENTS)
        tight = copy.deepcopy(POLICY)
        tight['bounds']['max_entry_bytes'] = len(aj.canonical_bytes(DOCUMENT)) - 1
        with self.assertRaises(aj.IntegrityError):
            ad.produce(tight, CONTEXT, {'kind': 'genesis'}, EVENTS)
        tight = copy.deepcopy(POLICY)
        tight['bounds']['max_entry_bytes'] = tight['bounds']['max_archive_bytes'] = len(aj.canonical_bytes(DOCUMENT))
        with self.assertRaises(aj.IntegrityError):  # ZIP framing pushes the archive over its bound
            ad.produce(tight, CONTEXT, {'kind': 'genesis'}, EVENTS)
        with self.assertRaises(aj.JournalError):
            ad.build_archive_bytes(POLICY, {'k': float('nan')})


class Opaque:
    """A value no canonical serializer accepts."""


def hostile_previous_documents():
    """Previous journal documents that pass the shape checks but blow up inside the
    shared canonicalizer (ValueError for NaN/inf, TypeError for mixed keys or
    unserializable values) before any typed rule can reject them."""
    def doc(**extra):
        return {**copy.deepcopy(DOCUMENT), **extra}
    nan_record = copy.deepcopy(DOCUMENT)
    nan_record['records'][0]['candidate']['pr'] = float('nan')
    return {
        'nan value': doc(note=float('nan')), 'infinite value': doc(note=float('inf')),
        'nan inside a record': nan_record,
        'mixed key types': {**copy.deepcopy(DOCUMENT), 1: 'x'}, 'none key': {**copy.deepcopy(DOCUMENT), None: 'x'},
        'unserializable value': doc(note=Opaque()), 'bytes value': doc(note=b'x'), 'set value': doc(note={1}),
    }


class HostilePreviousEvidenceTests(unittest.TestCase):
    """Previous evidence outside the canonical grammar must reject as typed evidence,
    never escape as an interpreter exception and never default to empty or genesis."""

    def assert_normalized(self, ctx):
        self.assertIsInstance(ctx.exception, aj.EvidenceError)
        self.assertIn('previous evidence', str(ctx.exception))
        # The rejection really is the adapter boundary normalizing the shared helper's
        # exception, not some earlier typed rule that happened to fire first.
        self.assertIsInstance(ctx.exception.__cause__, (TypeError, ValueError))
        self.assertNotIsInstance(ctx.exception.__cause__, aj.EvidenceError)

    def test_producer_rejects_hostile_previous_as_journal_evidence(self):
        for name, document in hostile_previous_documents().items():
            with self.subTest(name=name):
                with self.assertRaises(aj.JournalError) as ctx:
                    ad.build_journal_document(POLICY, CONTEXT, {'kind': 'journal', 'document': document}, [])
                self.assert_normalized(ctx)
        # A hostile previous evidence *container* (mixed keys around a valid document)
        # trips the shared validator's own sorted() key comparison; still typed here.
        for previous in ({'kind': 'journal', 'document': copy.deepcopy(DOCUMENT), 1: 'x'}, {'kind': 'genesis', 1: 'x'}):
            with self.subTest(previous=sorted(map(str, previous))), self.assertRaises(aj.JournalError) as ctx:
                ad.build_journal_document(POLICY, CONTEXT, previous, [])
            self.assertIsInstance(ctx.exception, aj.EvidenceError)
        # Unsupported and lost previous evidence keep the validator's own typed message
        # (the normalizer passes EvidenceError subclasses through unchanged).
        with self.assertRaises(aj.JournalError) as ctx:
            ad.build_journal_document(POLICY, CONTEXT, {'kind': 'snapshot', 'document': copy.deepcopy(DOCUMENT)}, [])
        self.assertIn('unsupported kind', str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)
        with self.assertRaises(aj.JournalError) as ctx:
            ad.build_journal_document(POLICY, CONTEXT, {'kind': 'unavailable', 'reason': 'expired'}, [])
        self.assertIn('no fresh genesis', str(ctx.exception))

    CHAINED = {'previous': {'kind': 'journal', 'document': DOCUMENT},
               'events': [{'operation_id': 'ef' * 16, 'candidate': MOVED_HEAD, 'operation': 'label-post', 'state': 'reserved'}]}

    def test_consumer_rejects_hostile_previous_after_one_download_without_retry(self):
        # The served journal is a genuine sequence-2 successor of the fixture document, so
        # the validator's chain check gets as far as canonicalizing the (hostile) previous
        # document instead of stopping at an earlier sequence rule.
        for name, document in hostile_previous_documents().items():
            with self.subTest(name=name):
                s = Scenario(tempting=True, **self.CHAINED)
                s.previous = {'kind': 'journal', 'document': document}
                with self.assertRaises(aj.JournalError) as ctx:
                    s.consume()
                self.assert_normalized(ctx)
                self.assertEqual(s.transport.methods(), FULL_SEQUENCE)  # observed twice, downloaded once, no retry
                s.transport.assert_read_only(self)
        s = Scenario(**self.CHAINED)
        s.previous = {'kind': 'journal', 'document': copy.deepcopy(DOCUMENT), 1: 'x'}
        with self.assertRaises(aj.JournalError):
            s.consume()
        self.assertEqual(s.transport.counts['download_artifact'], 1)
        # Control: the same chained scenario with intact previous evidence validates
        # diagnostically, so the rejections above are caused by the hostile previous alone.
        s = Scenario(**self.CHAINED)
        self.assertEqual(s.consume()['chain'], 'extended')

    def test_normalizer_preserves_evidence_subclasses_and_only_wraps_interpreter_errors(self):
        for error in (aj.IntegrityError, aj.ProvenanceError, aj.JournalError):
            def raise_it(error=error):
                raise error('typed')
            with self.assertRaises(error) as ctx:
                ad._normalized(raise_it, aj.JournalError, 'wrapped')
            self.assertEqual(str(ctx.exception), 'typed')
        for exc in (TypeError('t'), ValueError('v'), KeyError('k')):
            def raise_it(exc=exc):
                raise exc
            with self.assertRaises(aj.ProvenanceError) as ctx:
                ad._normalized(raise_it, aj.ProvenanceError, 'wrapped')
            self.assertIs(ctx.exception.__cause__, exc)
        self.assertEqual(ad._normalized(lambda: 'value', aj.JournalError, 'unused'), 'value')
        # Anything else (a bug, a MemoryError, a KeyboardInterrupt) is not evidence and
        # is not swallowed into a typed rejection.
        with self.assertRaises(RuntimeError):
            ad._normalized(lambda: (_ for _ in ()).throw(RuntimeError('bug')), aj.JournalError, 'wrapped')


class ConsumerTests(unittest.TestCase):
    def assert_no_authority(self, result):
        self.assertIs(result['admitted'], False)
        self.assertIs(result['dispatch_allowed'], False)
        self.assertEqual(result['side_effects'], 'rejected')
        self.assertEqual(result['stage'], aj.STAGE)

    def test_sequencing_over_produced_bytes_with_and_without_pagination(self):
        for page_size, pages in ((100, 1), (1, 2)):
            with self.subTest(page_size=page_size):
                s = Scenario(page_size=page_size, tempting=True)
                result = s.consume()
                self.assertTrue(result['diagnostic_valid'])
                self.assert_no_authority(result)
                self.assertEqual(result['artifact_digest'], s.produced['expected_artifact']['digest'])
                self.assertEqual(result['journal_digest'], s.produced['journal_digest'])
                self.assertEqual(result['semantic_state'], FIXTURE['expected']['semantic_state'])
                self.assertEqual(result['pr_unresolved_operations'], FIXTURE['expected']['pr_unresolved_operations'])
                expected = []
                for method in FULL_SEQUENCE:
                    expected.extend([method] * (pages if method.startswith('list_') else 1))
                self.assertEqual(s.transport.methods(), expected)
                downloads = [call for call in s.transport.calls if call[0] == 'download_artifact']
                self.assertEqual(downloads, [('download_artifact', POLICY['repository'], s.journal_artifact()['id'],
                                              s.journal_artifact()['size_in_bytes'])])
                s.transport.assert_read_only(self)
        # Projection drops REST noise but never invents a missing field.
        s = Scenario()
        del s.state['run_by_id'][FIXTURE['snapshot']['run']['id']]['triggering_actor']
        with self.assertRaises(aj.ProvenanceError):
            s.consume()
        s = Scenario()
        del s.state['runs'][0]['repository']['full_name']
        with self.assertRaises(aj.ProvenanceError):
            s.consume()

    def test_passing_adapters_cannot_authenticate_inputs_or_authorize(self):
        # The fake state is fully forged and self-consistent; the adapter has no way to
        # know, and its result carries no authority either way.
        s = Scenario()
        s.state['ref'] = s.policy['policy_commit']
        result = s.consume()
        self.assertTrue(result['diagnostic_valid'])
        self.assert_no_authority(result)
        text = json.dumps(result)
        for forbidden in ('"admitted": true', '"dispatch_allowed": true', '"admitted": "true"', '"dispatch_allowed": "true"'):
            self.assertNotIn(forbidden, text)

    def test_incomplete_pagination_rejects_before_download(self):
        class Endless(Transport):
            def _page(self, method, items, page):
                return self._reply(method, {'items': items[:1], 'has_next': True})

        s = Scenario()
        s.transport = Endless(s.state, s.archive)
        with self.assertRaises(aj.ProvenanceError) as ctx:
            s.consume()
        self.assertIn('incomplete', str(ctx.exception))
        self.assertEqual(s.transport.methods(), ['get_ref'] + ['list_workflow_runs'] * ad.MAX_LIST_PAGES)
        # Incomplete artifact listing, after a complete run listing.
        class EndlessArtifacts(Transport):
            def list_artifacts(self, repository, run_id, page):
                self.calls.append(('list_artifacts', repository, run_id, page))
                return self._reply('list_artifacts', {'items': [], 'has_next': True})

        s = Scenario()
        s.transport = EndlessArtifacts(s.state, s.archive)
        with self.assertRaises(aj.ProvenanceError) as ctx:
            s.consume()
        self.assertIn('incomplete', str(ctx.exception))
        self.assertNotIn('download_artifact', s.transport.methods())
        # An oversized page is unavailable, not silently accepted.
        s = Scenario()
        s.state['runs'] = s.state['runs'] * (ad.MAX_PAGE_ITEMS // 2 + 1)
        s.transport.page_size = ad.MAX_PAGE_ITEMS + 1
        with self.assertRaises(aj.ProvenanceError) as ctx:
            s.consume()
        self.assertIn('oversized', str(ctx.exception))
        self.assertNotIn('download_artifact', s.transport.methods())

    def test_unavailable_replies_block_without_retry_or_fallback(self):
        for method in ad.READ_METHODS:
            for reason in ('http-403', 'http-404', 'http-410', 'http-429 rate limited', 'timeout', '', 'x' * 500, 7, None):
                with self.subTest(method=method, reason=reason):
                    s = Scenario(tempting=True)
                    s.transport.unavailable[method] = reason
                    with self.assertRaises(aj.ProvenanceError) as ctx:
                        s.consume()
                    self.assertIn('evidence unavailable', str(ctx.exception))
                    if isinstance(reason, str) and 0 < len(reason) < 80:
                        self.assertIn(reason, str(ctx.exception))
                    self.assertEqual(s.transport.counts[method], 1)  # no retry
                    self.assertLessEqual(s.transport.counts.get('download_artifact', 0), 1)
                    self.assertEqual(s.transport.methods(), FULL_SEQUENCE[:FULL_SEQUENCE.index(method) + 1])
                    s.transport.assert_read_only(self)
        # Second page of a listing failing is as blocking as the first; the first page is
        # never used as a partial listing.
        s = Scenario(page_size=1)
        s.transport.unavailable['list_workflow_runs'], s.transport.unavailable_after['list_workflow_runs'] = 'http-403', 1
        with self.assertRaises(aj.ProvenanceError):
            s.consume()
        self.assertEqual(s.transport.methods(), ['get_ref', 'list_workflow_runs', 'list_workflow_runs'])
        # After-snapshot failures block even though the download succeeded.
        for method in ('get_ref', 'list_workflow_runs', 'get_workflow_run', 'list_artifacts'):
            with self.subTest(after=method):
                s = Scenario()
                s.transport.unavailable[method], s.transport.unavailable_after[method] = 'http-403', 1
                with self.assertRaises(aj.ProvenanceError):
                    s.consume()
                self.assertEqual(s.transport.counts['download_artifact'], 1)
                self.assertEqual(s.transport.counts[method], 2)

    def test_transport_exceptions_and_malformed_replies_fail_closed(self):
        class Broken(Transport):
            def get_workflow_run(self, repository, run_id):
                self.calls.append(('get_workflow_run', repository, run_id))
                raise ConnectionError('synthetic')

        s = Scenario()
        s.transport = Broken(s.state, s.archive)
        with self.assertRaises(aj.ProvenanceError) as ctx:
            s.consume()
        self.assertIn('transport error: ConnectionError', str(ctx.exception))
        self.assertEqual(s.transport.methods(), ['get_ref', 'list_workflow_runs', 'get_workflow_run'])
        malformed_replies = {'bare value': 'a' * 40, 'both keys': {'ok': 'a' * 40, 'unavailable': 'x'},
                             'extra key': {'ok': 'a' * 40, 'comment': 'fallback'}, 'none': None, 'list': [{'ok': 1}],
                             # Mixed-type keys would make sorted(reply) raise TypeError; they are
                             # malformed replies, not an escape and not a retry.
                             'mixed key types': {'ok': 'a' * 40, 1: 'x'}, 'int key only': {1: 'a' * 40},
                             'none key': {None: 'a' * 40, 'ok': 'a' * 40}, 'tuple key': {('ok',): 'a' * 40},
                             'empty dict': {}}
        for name, reply in malformed_replies.items():
            with self.subTest(name=name):
                s = Scenario()
                calls = []
                s.transport.get_ref = lambda repository, branch, reply=reply: (calls.append(1), reply)[1]
                with self.assertRaises(aj.ProvenanceError) as ctx:
                    s.consume()
                self.assertIn('malformed', str(ctx.exception))
                self.assertEqual(len(calls), 1)  # no retry
                self.assertEqual(s.transport.methods(), [])  # nothing past the malformed ref read
                self.assertEqual(ad._call(s.transport, 'get_ref', 'r', 'b'), (None, 'transport reply malformed'))
        malformed_pages = {'items not list': {'items': {}, 'has_next': False}, 'string has_next': {'items': [], 'has_next': 'false'},
                           'missing has_next': {'items': []}, 'extra key': {'items': [], 'has_next': False, 'total': 2},
                           'mixed key types': {'items': [], 'has_next': False, 1: 'x'}, 'int key': {0: [], 'has_next': False},
                           'none key': {None: [], 'has_next': False}}
        for name, page in malformed_pages.items():
            with self.subTest(page=name):
                s = Scenario()
                calls = []
                s.transport.list_artifacts = lambda repository, run_id, page_, page=page: (calls.append(1), {'ok': page})[1]
                with self.assertRaises(aj.ProvenanceError) as ctx:
                    s.consume()
                self.assertIn('malformed', str(ctx.exception))
                self.assertEqual(len(calls), 1)  # a malformed page is not re-requested
                self.assertNotIn('download_artifact', s.transport.methods())
        s = Scenario()  # missing transport method is unavailable evidence, not an AttributeError escape
        s.transport = object()
        with self.assertRaises(aj.ProvenanceError):
            s.consume()
        s = Scenario()  # an empty complete listing selects nothing
        s.state['runs'] = []
        with self.assertRaises(aj.ProvenanceError) as ctx:
            s.consume()
        self.assertIn('no run listed', str(ctx.exception))

    def test_moving_snapshots_between_before_and_after_block(self):
        run_id = FIXTURE['snapshot']['run']['id']
        newer = noisy({**FIXTURE['snapshot']['run'], 'id': run_id + 1})

        def artifact(state):
            return next(m for m in state['artifacts'][run_id] if m['name'] == POLICY['artifact_name'])

        moves = {
            'default branch moved': lambda st: st.update(ref=OTHER_SHA),
            'newer run appeared': lambda st: (st['runs'].insert(0, newer), st['run_by_id'].update({run_id + 1: newer})),
            'artifact updated': lambda st: artifact(st).update(updated_at='2026-10-16T12:00:01Z'),
            'artifact digest changed': lambda st: artifact(st).update(digest='sha256:' + 'e' * 64),
            'artifact deleted': lambda st: st['artifacts'][run_id].remove(artifact(st)),
            'artifact expired': lambda st: artifact(st).update(expired=True),
            'run rerun': lambda st: st['run_by_id'][run_id].update(run_attempt=2),
        }
        for name, move in moves.items():
            with self.subTest(name=name):
                s = Scenario(tempting=True)
                s.transport.on_download = move
                with self.assertRaises(aj.ProvenanceError):
                    s.consume()
                self.assertEqual(s.transport.methods(), FULL_SEQUENCE)  # observed once more, never retried
                s.transport.assert_read_only(self)
        s = Scenario()
        with self.assertRaises(aj.ProvenanceError):
            s.consume(before=AFTER_AT, after=BEFORE_AT)
        s = Scenario()
        with self.assertRaises(aj.ProvenanceError):
            s.consume(after='2026-10-16T12:00:05')  # unqualified timestamp

    def test_deleted_expired_newer_and_unauthoritative_evidence_is_never_downloaded(self):
        run_id = FIXTURE['snapshot']['run']['id']
        newer = noisy({**FIXTURE['snapshot']['run'], 'id': run_id + 1, 'status': 'in_progress', 'conclusion': None})
        cases = {
            'artifact deleted': lambda s: s.state['artifacts'].__setitem__(run_id, []),
            'artifact missing': lambda s: s.state['artifacts'][run_id].remove(s.journal_artifact()),
            'expired flag': lambda s: s.journal_artifact().update(expired=True),
            'expired before observation': lambda s: s.journal_artifact().update(expires_at='2026-10-16T11:59:59Z'),
            'expires at observation': lambda s: s.journal_artifact().update(expires_at=BEFORE_AT),
            'null digest': lambda s: s.journal_artifact().update(digest=None),
            'size over bound': lambda s: s.journal_artifact().update(size_in_bytes=POLICY['bounds']['max_archive_bytes'] + 1),
            'duplicate artifact name': lambda s: s.state['artifacts'][run_id].append({**copy.deepcopy(s.journal_artifact()), 'id': 9}),
            'newer pending run': lambda s: (s.state['runs'].insert(0, newer), s.state['run_by_id'].update({run_id + 1: newer})),
            'selected run failed': lambda s: s.state['run_by_id'][run_id].update(conclusion='failure'),
            'selected run differs from listing': lambda s: s.state['run_by_id'][run_id].update(created_at='2026-10-16T11:49:00Z'),
            'listing has candidate event': lambda s: s.state['runs'][1].update(event='pull_request'),
            'default branch moved': lambda s: s.state.update(ref=OTHER_SHA),
            'fork head repository': lambda s: s.state['run_by_id'][run_id]['head_repository'].update(id=1),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                s = Scenario(tempting=True)
                mutate(s)
                with self.assertRaises(aj.ProvenanceError):
                    s.consume()
                self.assertNotIn('download_artifact', s.transport.methods())
                s.transport.assert_read_only(self)

    def test_invalid_digest_and_corrupt_archives_are_integrity_failures(self):
        other = ad.produce(POLICY, CONTEXT, {'kind': 'genesis'}, EVENTS[:3])['archive']
        intact = Scenario().archive
        flipped = bytearray(intact)
        flipped[flipped.find(b'{"') + 1] ^= 0x01
        cases = {
            'different journal bytes': (other, None),
            'truncated': (intact[:-7], None),
            'appended byte': (intact + b'\x00', None),
            'not a zip': (b'PK\x03\x04' + b'\x00' * 40, None),
            'empty bytes': (b'', None),
            'string body': (intact.decode('latin-1'), None),
            'honest metadata for corrupted entry': (bytes(flipped), ad.expected_artifact(bytes(flipped))),
            'honest metadata for a second entry': (None, 'two-entries'),
        }
        for name, (archive, meta) in cases.items():
            with self.subTest(name=name):
                s = Scenario(tempting=True)
                if meta == 'two-entries':
                    buffer = io.BytesIO(intact)
                    with zipfile.ZipFile(buffer, 'a') as container:
                        container.writestr('notes.txt', b'x')
                    archive, meta = buffer.getvalue(), ad.expected_artifact(buffer.getvalue())
                s.transport.archive = archive
                if meta is not None:
                    s.journal_artifact().update(meta)
                with self.assertRaises(aj.IntegrityError):
                    s.consume()
                self.assertEqual(s.transport.counts['download_artifact'], 1)
                # A body longer than the authenticated cap is rejected right after the
                # download; every other defect is found by the validator after the
                # after-snapshot. Neither path retries.
                over_cap = isinstance(archive, bytes) and len(archive) > s.journal_artifact()['size_in_bytes']
                self.assertEqual(s.transport.methods(), FULL_SEQUENCE[:5] if over_cap else FULL_SEQUENCE)
                s.transport.assert_read_only(self)
        # Zero-byte authenticated metadata rejects at provenance before any download.
        s = Scenario()
        s.journal_artifact().update(size_in_bytes=0)
        with self.assertRaises(aj.ProvenanceError):
            s.consume()
        self.assertNotIn('download_artifact', s.transport.methods())

    def test_pr_level_holds_survive_candidate_policy_and_evidence_movement(self):
        A = DOCUMENT['records'][0]['operation_id']
        for name, candidate in {'head moved': MOVED_HEAD, 'base moved': {**CANDIDATE, 'base': 'c9' * 20, 'merge': 'da' * 20, 'tree': 'eb' * 20},
                                'merge moved': {**CANDIDATE, 'merge': 'da' * 20}, 'tree moved': {**CANDIDATE, 'tree': 'eb' * 20}}.items():
            with self.subTest(name=name):
                s = Scenario()
                s.candidate = candidate
                result = s.consume()
                self.assertEqual(result['semantic_state'], 'pr-unresolved')
                self.assertEqual(result['candidate_state'], 'not-dispatched')
                self.assertTrue(result['operator_reconciliation_required'])
                self.assertEqual(result['pr_unresolved_operations'], [A])
                self.assert_no_authority(result)
        # Another PR in the repository is isolated; a foreign-repository candidate never reads anything.
        s = Scenario()
        s.candidate = FIXTURE['other_candidates']['unrecorded']['candidate']
        result = s.consume()
        self.assertEqual((result['semantic_state'], result['pr_unresolved_operations']), ('not-dispatched', []))
        s = Scenario()
        s.candidate = {**CANDIDATE, 'repo': 'example-org/fork'}
        with self.assertRaises(aj.JournalError):
            s.consume()
        self.assertEqual(s.transport.calls, [])
        # A chained successor produced for the moved head still holds the old unknown outcome.
        chained = Scenario(previous={'kind': 'journal', 'document': copy.deepcopy(DOCUMENT)}, context={**CONTEXT, 'run_id': CONTEXT['run_id']},
                           events=[{'operation_id': 'ef' * 16, 'candidate': MOVED_HEAD, 'operation': 'label-post', 'state': 'reserved'}])
        chained.candidate = MOVED_HEAD
        result = chained.consume()
        self.assertEqual((result['chain'], result['journal_sequence'], result['semantic_state']), ('extended', 2, 'reserved'))
        self.assertTrue(result['operator_reconciliation_required'])
        self.assertEqual(result['pr_unresolved_operations'], sorted([A, 'ef' * 16]))
        # Policy / source-closure movement: the trusted policy is checked before any read.
        s = Scenario()
        s.policy['source_closure']['commit'] = OTHER_SHA
        with self.assertRaises(aj.ProvenanceError):
            s.consume()
        self.assertEqual(s.transport.calls, [])
        s = Scenario()
        s.policy['policy_commit'] = s.policy['source_closure']['commit'] = OTHER_SHA
        with self.assertRaises(aj.ProvenanceError):
            s.consume()
        self.assertNotIn('download_artifact', s.transport.methods())
        # Lost previous evidence is operator reconciliation; the download is not retried.
        for previous in ({'kind': 'unavailable', 'reason': 'artifact expired'}, {'kind': 'unavailable'}, None):
            with self.subTest(previous=previous):
                s = Scenario()
                s.previous = previous
                with self.assertRaises(aj.JournalError):
                    s.consume()
                self.assertEqual(s.transport.counts['download_artifact'], 1)
        # A sequence-1 journal after real history is a manufactured genesis and rejects.
        s = Scenario()
        s.previous = {'kind': 'journal', 'document': {**copy.deepcopy(DOCUMENT), 'records': copy.deepcopy(DOCUMENT['records'][:3])}}
        with self.assertRaises(aj.JournalError):
            s.consume()
        self.assertEqual(s.transport.counts['download_artifact'], 1)

    def test_download_carries_the_authenticated_size_as_an_explicit_byte_cap(self):
        s = Scenario(tempting=True)
        s.consume()
        (download,) = [call for call in s.transport.calls if call[0] == 'download_artifact']
        cap = download[3]
        self.assertEqual(download, ('download_artifact', POLICY['repository'], s.journal_artifact()['id'], cap))
        self.assertIs(type(cap), int)
        self.assertEqual(cap, s.journal_artifact()['size_in_bytes'])
        self.assertEqual(cap, len(s.archive))
        self.assertLessEqual(cap, POLICY['bounds']['max_archive_bytes'])
        # The cap follows the authenticated metadata, not the body: shrink the declared
        # size and the (unchanged) body now exceeds the cap. Rejected before the
        # after-snapshot, as an integrity failure, with no second download.
        s = Scenario(tempting=True)
        s.journal_artifact().update(size_in_bytes=len(s.archive) - 1)
        with self.assertRaises(aj.IntegrityError) as ctx:
            s.consume()
        self.assertIn('exceed the authenticated size cap', str(ctx.exception))
        self.assertEqual(s.transport.methods(), FULL_SEQUENCE[:5])
        self.assertEqual(s.transport.calls[-1][3], len(s.archive) - 1)
        s.transport.assert_read_only(self)
        # A transport that ignores the cap and returns a larger body is still rejected
        # here; the adapter cannot stop the allocation, only refuse the result.
        for extra in (b'\x00', b'\x00' * 4096, b'\x00' * (POLICY['bounds']['max_archive_bytes'] + 1)):
            with self.subTest(extra=len(extra)):
                s = Scenario()
                s.transport.archive = s.archive + extra
                with self.assertRaises(aj.IntegrityError) as ctx:
                    s.consume()
                self.assertIn('size cap', str(ctx.exception))
                self.assertEqual(s.transport.methods(), FULL_SEQUENCE[:5])
        # A bytearray body is capped the same way; a non-bytes body is left to the
        # validator's bytes-required rule (still integrity, still no retry).
        s = Scenario()
        s.transport.archive = bytearray(s.archive) + b'\x00'
        with self.assertRaises(aj.IntegrityError) as ctx:
            s.consume()
        self.assertIn('size cap', str(ctx.exception))
        s = Scenario()
        s.transport.archive = [0] * (len(s.archive) + 1)
        with self.assertRaises(aj.IntegrityError) as ctx:
            s.consume()
        self.assertIn('bytes required', str(ctx.exception))
        self.assertEqual(s.transport.counts['download_artifact'], 1)
        # A transport built for the old two-argument contract cannot be called: the
        # TypeError is unavailable evidence, so the bound can never be silently dropped.
        class Legacy(Transport):
            def download_artifact(self, repository, artifact_id):
                self.calls.append(('download_artifact', repository, artifact_id))
                return self._reply('download_artifact', self.archive)

        s = Scenario()
        s.transport = Legacy(s.state, s.archive)
        with self.assertRaises(aj.ProvenanceError) as ctx:
            s.consume()
        self.assertIn('download transport error: TypeError', str(ctx.exception))
        self.assertEqual(s.transport.methods(), FULL_SEQUENCE[:4])  # the legacy body never ran

    def test_call_helper_enforces_the_read_allowlist_at_runtime(self):
        s = Scenario(tempting=True)
        for name in SIDE_EFFECTS + ('methods', '_reply', '__class__', '__init__', 'download_artifacts', 'GET_REF', ''):
            with self.subTest(method=name):
                value, reason = ad._call(s.transport, name, POLICY['repository'], 1)
                self.assertIsNone(value)
                self.assertIn('outside the read allowlist', reason)
        self.assertEqual(s.transport.calls, [])  # no attribute on the transport was reached
        s.transport.assert_read_only(self)
        for name in ad.READ_METHODS:
            self.assertTrue(hasattr(s.transport, name), name)
        self.assertEqual(ad._call(s.transport, 'get_ref', POLICY['repository'], POLICY['default_branch']), (s.state['ref'], None))
        self.assertEqual(s.transport.methods(), ['get_ref'])
        # A transport whose attribute lookup itself is hostile is still only reached
        # for allowlisted names, and its failure is unavailable evidence.
        class Hostile:
            def __getattr__(self, name):
                raise RuntimeError('lookup of ' + name)

        value, reason = ad._call(Hostile(), 'post_comment')
        self.assertEqual((value, reason), (None, 'adapter refused a method outside the read allowlist'))
        value, reason = ad._call(Hostile(), 'get_ref')
        self.assertEqual((value, reason), (None, 'transport error: RuntimeError'))
        # Every reason the adapter mints fits the validator's reason grammar, so it is
        # reported verbatim rather than collapsing to 'unspecified'.
        for reason in ('transport reply malformed', 'listing page malformed or oversized',
                       'adapter refused a method outside the read allowlist', 'no run listed for the workflow',
                       'transport error: ' + 'X' * 63):
            self.assertIsNotNone(aj.REASON.fullmatch(reason), reason)

    def test_observe_shapes_are_validator_shapes(self):
        s = Scenario()
        before = ad.observe(s.transport, s.policy, BEFORE_AT)
        self.assertEqual(sorted(before), sorted(aj.SNAPSHOT_KEYS))
        self.assertEqual(before['run'], FIXTURE['snapshot']['run'])
        self.assertEqual(before['workflow_runs'], FIXTURE['snapshot']['workflow_runs'])
        self.assertEqual([m['name'] for m in before['artifacts']], [m['name'] for m in FIXTURE['snapshot']['artifacts']])
        self.assertNotIn('trusted', json.dumps(before))
        s.transport.unavailable['get_ref'] = 'http-403'
        self.assertEqual(ad.observe(s.transport, s.policy, BEFORE_AT), {'unavailable': 'http-403'})


class DisabledEntryTests(unittest.TestCase):
    KNOBS = {'CANDIDATE_ARTIFACT_ADAPTERS_ENABLED': '1', 'ARTIFACT_JOURNAL_ACTIVATE': 'true', 'ENABLE_SIDE_EFFECTS': 'yes',
             'GITHUB_EVENT_NAME': 'schedule', 'GITHUB_REF': 'refs/heads/main', 'GH_TOKEN': 'synthetic-not-a-token'}

    def test_disabled_entry_never_invokes_transport_or_callbacks(self):
        s = Scenario(tempting=True)
        side_effect = mock.Mock()
        for payload in (None, {}, {'enable': True}, {'dispatch_allowed': True, 'admitted': True}, s.produced, s.consume()):
            with self.subTest(payload=type(payload).__name__):
                result = ad.disabled_entry(payload, environ=self.KNOBS, transport=s.transport, upload=side_effect,
                                           dispatch=side_effect, post=side_effect, activate=True)
                self.assertEqual((result['admitted'], result['dispatch_allowed'], result['side_effects']), ('false', 'false', 'rejected'))
                self.assertEqual(result['action'], 'artifact-adapters-disabled')
                self.assertEqual(result['stage'], ad.STAGE)
        side_effect.assert_not_called()
        s.transport.assert_read_only(self)
        self.assertEqual(s.transport.methods(), FULL_SEQUENCE)  # only the explicit consume() above touched it
        self.assertIsNot(ad.disabled_entry(), ad.DISABLED)
        output = io.StringIO()
        with mock.patch.dict(os.environ, self.KNOBS), contextlib.redirect_stdout(output):
            code = ad.main(['--activate', '--upload', '--dispatch'], self.KNOBS)
        self.assertEqual(code, 1)
        self.assertIn('artifact-adapters-disabled', output.getvalue())
        self.assertNotIn('synthetic-not-a-token', output.getvalue())
        # The producer result is inert data: nothing in it is callable or uploaded.
        self.assertFalse(any(callable(v) for v in s.produced.values()))
        self.assertIs(s.produced['uploaded'], False)

    ALLOWED_IMPORTS = {'__future__', 'copy', 'hashlib', 'io', 'zipfile', 'candidate_artifact_journal'}
    FORBIDDEN_NAMES = {'open', 'exec', 'eval', 'compile', '__import__'}
    FORBIDDEN_ATTRIBUTES = {'environ', 'getenv', 'getenvb', 'extractall', 'extract', 'system', 'popen', 'Popen', 'urlopen',
                            'now', 'utcnow', 'post', 'put', 'patch', 'delete', 'request'}

    def test_module_exposes_no_activation_switch_transport_clock_or_extraction(self):
        names = [n for n in dir(ad) if any(w in n.lower() for w in ('enable', 'activat', 'token', 'secret', 'upload', 'dispatch_'))]
        self.assertEqual(names, [])
        for module in ('os', 'sys', 'subprocess', 'socket', 'urllib', 'http', 'ssl', 'tempfile', 'shutil', 'pathlib', 'datetime', 'time'):
            self.assertFalse(hasattr(ad, module), module)
        tree = ast.parse(Path(ad.__file__).read_text(encoding='utf-8'))
        imported, called = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or '').split('.')[0])
            elif isinstance(node, ast.Name):
                self.assertNotIn(node.id, self.FORBIDDEN_NAMES, node.id)
            elif isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, self.FORBIDDEN_ATTRIBUTES, node.attr)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'getattr':
                called.add(node.args[1].id if isinstance(node.args[1], ast.Name) else None)
        self.assertLessEqual(imported, self.ALLOWED_IMPORTS, imported - self.ALLOWED_IMPORTS)
        # Structural hint only: the single getattr call takes the variable ``method``.
        # That does not prove what ``method`` holds; the runtime allowlist check in
        # ``_call`` (test_call_helper_enforces_the_read_allowlist_at_runtime) does.
        self.assertEqual(called, {'method'})
        self.assertEqual(set(ad.READ_METHODS), {'get_ref', 'list_workflow_runs', 'get_workflow_run', 'list_artifacts', 'download_artifact'})

    def test_existing_gate_workflows_and_validator_are_not_wired_to_the_adapters(self):
        for path in ('scripts/ci/candidate_gate.py', 'scripts/ci/merge_candidate.py', 'scripts/ci/codex_gate.py',
                     'scripts/ci/claude_fixer_receipts.py', 'scripts/ci/candidate_artifact_journal.py'):
            self.assertNotIn('candidate_artifact_adapters', (ROOT / path).read_text(encoding='utf-8'), path)
        for workflow in (ROOT / '.github/workflows').glob('*.yml'):
            self.assertNotIn('candidate_artifact_adapters', workflow.read_text(encoding='utf-8'), workflow.name)


if __name__ == '__main__':
    unittest.main()
