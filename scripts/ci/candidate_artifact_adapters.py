#!/usr/bin/env python3
"""Producer/consumer adapters for the artifact journal, v1 PREPARATION (disabled, fail-closed).

Two offline, pure adapters around ``candidate_artifact_journal`` (the validator):

producer   ``build_journal_document`` / ``build_archive_bytes`` / ``produce``
           Deterministic envelope and single-entry ZIP assembly from a reviewed trusted
           policy, a producer run context and appended journal events. The produced
           bytes are re-verified through the validator before they are returned, so a
           producer can never emit an archive the consumer would reject.
consumer   ``observe`` / ``consume``
           Sequencing over an *injected* read-only transport: before-snapshot, bounded
           pagination, download of exactly the authenticated artifact, after-snapshot,
           then ``evaluate_offline``. Every unavailable, malformed or moving reply fails
           closed through the validator's own rules. There is no retry and no fallback to
           bot comments, older runs or partial listings.

Nothing here performs network access, uploads, dispatches, reads credentials or the
environment, or activates anything. ``disabled_entry`` rejects every payload and never
invokes a callback; ``main`` returns 1. A passing adapter proves mutual consistency of
caller-supplied inputs only: it cannot authenticate the transport, the policy, the
previous evidence or the clock, all of which the caller injects. See
docs/ci-artifact-adapters-staged.md for the owner decisions still required.

Stdlib plus the validator only. No os, sys, subprocess, urllib, tempfile or datetime.
"""
from __future__ import annotations

import copy
import hashlib
import io
import zipfile

import candidate_artifact_journal as aj

STAGE = 'artifact-adapters-v1-offline-preparation'
# Bounded pagination: at most MAX_LIST_PAGES pages of at most MAX_PAGE_ITEMS items each
# (the REST maximum per_page). Listings that do not finish inside the bound are
# ``complete: false`` and reject; they are never treated as a complete snapshot.
MAX_LIST_PAGES = 8
MAX_PAGE_ITEMS = 100
# The only transport methods the consumer calls; ``_call`` enforces this allowlist at
# runtime. Each returns {'ok': value} or {'unavailable': '<reason>'}; anything else,
# including an exception, is unavailable. ``download_artifact(repository, artifact_id,
# max_bytes)`` receives the authenticated size as an explicit byte cap it must enforce
# while streaming; the adapter rejects any returned body longer than the cap.
READ_METHODS = ('get_ref', 'list_workflow_runs', 'get_workflow_run', 'list_artifacts', 'download_artifact')
EVENT_KEYS = ('operation_id', 'candidate', 'operation', 'state')
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
RUN_NESTED = {'repository': ('id', 'full_name'), 'head_repository': ('id',),
              'actor': aj.ACTOR_KEYS, 'triggering_actor': aj.ACTOR_KEYS}
ARTIFACT_NESTED = {'workflow_run': aj.ARTIFACT_RUN_KEYS}

DISABLED = {'stage': STAGE, 'action': 'artifact-adapters-disabled', 'admitted': 'false',
            'dispatch_allowed': 'false', 'side_effects': 'rejected',
            'reason': 'v1 adapters are offline preparation; no transport, upload, dispatch or activation route exists'}


def _require(condition, error, message):
    if not condition:
        raise error(message)


def _exact(value, keys, error, what):
    # Set comparison, not sorted(): a dict with mixed-type keys must reject as evidence,
    # not escape as a TypeError from the sort.
    _require(isinstance(value, dict) and set(value) == set(keys), error, what + ': exact field set required')
    return value


def _normalized(call, error, message):
    """Run ``call``. An EvidenceError passes through unchanged. Any other TypeError,
    ValueError or KeyError that a shared helper raises over caller-supplied data (NaN,
    mixed-type keys, unserializable values, sort failures) becomes ``error`` so the
    caller fails closed on typed evidence instead of an interpreter exception. Nothing
    is defaulted: no empty document, no genesis."""
    try:
        return call()
    except aj.EvidenceError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise error(message) from exc


# --- producer: deterministic assembly from validated inputs ----------------

def validate_producer_context(context, policy):
    """Runtime facts the producer run asserts about itself, in the envelope's ``producer``
    shape. They must bind an already validated trusted policy; the adapter cannot check
    them against REST metadata (that is the consumer's job via ``validate_journal``)."""
    J = aj.JournalError
    _exact(context, aj.PRODUCER_KEYS, J, 'producer context')
    _require(type(context['run_id']) is int and context['run_id'] > 0, J, 'producer context: numeric run ID')
    _require(type(context['run_attempt']) is int and context['run_attempt'] == 1, J,
             'producer context: rerun attempts are unsupported in v1')
    _require(context['event'] in policy['producer_events'], J,
             'producer context: event is not a trusted producer event')
    _require(context['runtime_sha'] == policy['policy_commit'], J,
             'producer context: runtime GITHUB_SHA is not the default-branch policy commit')
    _require(context['workflow_ref'] == aj.workflow_ref(policy), J,
             'producer context: qualified workflow ref does not bind the trusted policy')
    return context


def build_journal_document(policy, context, previous, events):
    """Envelope from policy, producer context, previous evidence and appended events.

    ``events`` are ``{operation_id, candidate, operation, state}``; sequence numbers and
    the recording ``run_id`` are assigned here, continuing the previous journal's records
    unchanged. Previous evidence follows ``reconcile``: genesis starts sequence 1, a
    previous journal is extended by exactly one sequence, and lost evidence rejects.
    """
    policy = aj.validate_policy(policy)
    context = validate_producer_context(context, policy)
    J = aj.JournalError
    kind = previous.get('kind') if isinstance(previous, dict) else None
    prev = previous.get('document') if kind == 'journal' else None
    if kind == 'genesis':
        sequence, predecessor, records = 1, None, []
    elif isinstance(prev, dict) and isinstance(prev.get('records'), list) and type(prev.get('journal_sequence')) is int:
        # The previous document is canonicalized before the chain check; a NaN, mixed-type
        # key or unserializable value in it is malformed previous evidence, never a crash.
        predecessor = _normalized(lambda: aj.canonical_digest(prev), J, 'previous evidence: not canonically serializable')
        sequence, records = prev['journal_sequence'] + 1, copy.deepcopy(prev['records'])
    else:
        # Unavailable, unsupported or malformed previous evidence rejects here, with the
        # validator's own typed error where it has one.
        _normalized(lambda: aj.reconcile(previous, {}), J, 'previous evidence: malformed')
        raise J('previous evidence: unusable')
    _require(isinstance(events, list), J, 'journal events: list required')
    for event in events:
        _exact(event, EVENT_KEYS, J, 'journal event')
        records.append({'sequence': len(records) + 1, 'run_id': context['run_id'], **copy.deepcopy(event)})
    document = {'schema': aj.JOURNAL_SCHEMA, 'repository_id': policy['repository_id'],
                'repository': policy['repository'], 'policy_commit': policy['policy_commit'],
                'workflow_id': policy['workflow_id'], 'workflow_path': policy['workflow_path'],
                'producer': dict(context), 'journal_sequence': sequence,
                'predecessor_digest': predecessor, 'records': records}
    aj.validate_records(records, policy['repository'], policy['bounds']['max_records'], context['run_id'])
    _normalized(lambda: aj.reconcile(previous, document), J, 'previous evidence: not canonically comparable')
    return document


def expected_artifact(archive):
    """The size and digest the authenticated REST metadata must later show for ``archive``."""
    return {'size_in_bytes': len(archive), 'digest': 'sha256:' + hashlib.sha256(archive).hexdigest()}


def build_archive_bytes(policy, document):
    """Deterministic single stored entry named ``journal_entry``, fixed 1980 timestamp,
    regular-file mode, no comment. Re-verified through ``verify_archive`` so the bytes
    are exactly what the consumer will accept, within the policy bounds."""
    policy = aj.validate_policy(policy)
    try:
        payload = aj.canonical_bytes(document)
    except (TypeError, ValueError) as exc:
        raise aj.JournalError('journal document: not canonically serializable') from exc
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as container:
        info = zipfile.ZipInfo(policy['journal_entry'], date_time=ZIP_EPOCH)
        info.compress_type = zipfile.ZIP_STORED
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        container.writestr(info, payload)
    archive = buffer.getvalue()
    _require(aj.verify_archive(archive, expected_artifact(archive), policy) == document, aj.IntegrityError,
             'archive: produced entry does not round-trip to the document')
    return archive


def produce(policy, context, previous, events):
    """Pure producer result. ``uploaded`` is always False: no upload exists in v1."""
    document = build_journal_document(policy, context, previous, events)
    archive = build_archive_bytes(policy, document)
    return {'stage': STAGE, 'document': document, 'archive': archive,
            'journal_digest': aj.canonical_digest(document), 'expected_artifact': expected_artifact(archive),
            'uploaded': False}


# --- consumer: sequencing over an injected read-only transport -------------

def _call(transport, method, *args):
    """One read-only call. Returns (value, None) or (None, reason); never raises.

    ``method`` is checked against READ_METHODS at runtime before any attribute lookup,
    so no code path can reach a transport method outside the allowlist. Reply key sets
    are compared as sets: a dict with mixed-type keys is a malformed reply, not a
    TypeError escaping from a sort. There is no retry on any outcome."""
    if method not in READ_METHODS:
        return None, 'adapter refused a method outside the read allowlist'
    try:
        reply = getattr(transport, method)(*args)
    except Exception as exc:  # every transport failure is unavailable evidence, never an escape
        return None, ('transport error: ' + type(exc).__name__)[:80]
    keys = set(reply) if isinstance(reply, dict) else None
    if keys == {'ok'}:
        return reply['ok'], None
    if keys == {'unavailable'}:
        reason = reply['unavailable']
        return None, reason if isinstance(reason, str) and aj.REASON.fullmatch(reason) else 'unspecified'
    return None, 'transport reply malformed'


def _listing(transport, method, *args):
    """Bounded pagination. Returns (items, complete, reason)."""
    items = []
    for page in range(1, MAX_LIST_PAGES + 1):
        value, reason = _call(transport, method, *args, page)
        if reason is not None:
            return None, False, reason
        if not (isinstance(value, dict) and set(value) == {'has_next', 'items'} and isinstance(value['items'], list)
                and type(value['has_next']) is bool and len(value['items']) <= MAX_PAGE_ITEMS):
            return None, False, 'listing page malformed or oversized'
        items.extend(value['items'])
        if not value['has_next']:
            return items, True, None
    return items, False, None


def _project(raw, keys, nested):
    """REST object projected to the validator's exact field set. Unknown fields are
    dropped; missing fields stay missing so the validator rejects the exact set."""
    if not isinstance(raw, dict):
        return raw
    out = {}
    for key in keys:
        if key in raw:
            out[key] = _project(raw[key], nested[key], {}) if key in nested else raw[key]
    return out


def observe(transport, policy, observed_at):
    """One bounded snapshot in validator shape. ``observed_at`` is supplied by the caller
    (the adapter has no clock). Any unavailable or malformed reply makes the whole
    snapshot ``{'unavailable': reason}``; a listing that overruns the bound is
    ``complete: false``. Both reject in ``validate_snapshot``."""
    policy = aj.validate_policy(policy)
    repository = policy['repository']
    head, reason = _call(transport, 'get_ref', repository, policy['default_branch'])
    if reason is not None:
        return {'unavailable': reason}
    runs, complete, reason = _listing(transport, 'list_workflow_runs', repository, policy['workflow_id'])
    if reason is not None:
        return {'unavailable': reason}
    runs = [_project(entry, aj.RUN_KEYS, RUN_NESTED) for entry in runs]
    snapshot = {'schema': aj.SNAPSHOT_SCHEMA, 'observed_at': observed_at, 'complete': complete,
                'default_branch_head': head, 'run': {}, 'workflow_runs': runs, 'artifacts': []}
    if not complete:
        return snapshot
    ids = [entry['id'] for entry in runs if isinstance(entry, dict) and type(entry.get('id')) is int]
    if not ids:
        return {'unavailable': 'no run listed for the workflow'}
    newest = max(ids)  # the validator rejects unless this run is complete, successful and authoritative
    run, reason = _call(transport, 'get_workflow_run', repository, newest)
    if reason is not None:
        return {'unavailable': reason}
    snapshot['run'] = _project(run, aj.RUN_KEYS, RUN_NESTED)
    artifacts, complete, reason = _listing(transport, 'list_artifacts', repository, newest)
    if reason is not None:
        return {'unavailable': reason}
    snapshot['artifacts'] = [_project(meta, aj.ARTIFACT_KEYS, ARTIFACT_NESTED) for meta in artifacts]
    snapshot['complete'] = complete
    return snapshot


def consume(transport, policy, candidate, previous, observed_before, observed_after):
    """before-snapshot -> validate -> download exactly the authenticated artifact ->
    after-snapshot -> ``evaluate_offline``. Raises EvidenceError on any defect; the
    result otherwise carries ``diagnostic_valid`` and never any authority."""
    policy = aj.validate_policy(policy)
    aj.validate_candidate(candidate)
    _require(candidate['repo'] == policy['repository'], aj.JournalError,
             'candidate repository differs from the policy repository; nothing is read')
    before = observe(transport, policy, observed_before)
    _run, artifact = aj.validate_snapshot(before, policy)  # fails closed before any download
    # The download carries an explicit byte cap: the authenticated size, which
    # validate_snapshot has already bounded by the policy's max_archive_bytes. A real
    # transport must enforce this cap while streaming, before allocating the body; the
    # adapter can only reject a returned body that exceeds it, which it does here before
    # any further read.
    max_bytes = artifact['size_in_bytes']
    archive, reason = _call(transport, 'download_artifact', policy['repository'], artifact['id'], max_bytes)
    if reason is not None:
        raise aj.ProvenanceError('evidence unavailable: download ' + reason)
    _require(not isinstance(archive, (bytes, bytearray)) or len(archive) <= max_bytes, aj.IntegrityError,
             'archive: downloaded bytes exceed the authenticated size cap')
    after = observe(transport, policy, observed_after)
    # The validator's chain check canonicalizes the previous document; its interpreter
    # exceptions over hostile previous evidence become typed journal evidence here.
    return _normalized(lambda: aj.evaluate_offline(policy, before, after, archive, candidate, previous),
                       aj.JournalError, 'previous evidence: not canonically comparable')


# --- side-effect authority: always rejected ---------------------------------

def disabled_entry(*_ignored, **_also_ignored):
    """Production entry point. Arguments (payload, environment, transport, callbacks)
    are accepted so an accidental caller receives the rejection instead of a TypeError;
    none is read and no callback is ever invoked."""
    return dict(DISABLED)


def main(argv=None, environ=None):
    result = disabled_entry(argv, environ)
    print('::notice::' + result['action'] + ': ' + result['reason'])
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
