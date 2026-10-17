#!/usr/bin/env python3
"""Artifact-based candidate journal, v1 PREPARATION (disabled, fail-closed).

Offline verification of run-bound artifact evidence that a future trusted
default-branch schedule/manual reconciler would upload. Nothing here is wired
into a workflow and nothing here can authorize a side effect: ``evaluate_offline``
returns ``admitted=False`` and ``dispatch_allowed=False`` even when every input
validates, and ``disabled_entry`` rejects every payload, environment variable,
configuration key and callback. There is no activation switch; activation is a
separate owner decision that arrives as reviewed source, not as a flag.

Layers are kept separate and must stay separate (see docs/ci-artifact-journal-v1.md):

1. artifact integrity    ``verify_archive``: bytes, server digest, bounded ZIP, strict JSON
2. provenance            ``validate_policy``/``validate_snapshot``: independently supplied
                         trusted policy versus authenticated Actions metadata the caller
                         obtained through an existing-access transport
3. semantic journal      ``validate_journal``/``reconcile``/``journal_status``
4. side-effect authority ``stage``/``disabled_entry``: always rejected in v1

Caller trust. This module cannot authenticate its own inputs. The trusted policy
must be reviewed and supplied independently of the artifact; snapshots must come
from an authenticated REST transport, never from the archive's own assertions.
Arbitrary caller-supplied JSON is not proof, and a ``trusted`` flag inside the
envelope is an unknown field and is rejected. A successful run alone does not
prove trusted YAML or scripts; the source closure in the policy is the owner's
reviewed pin, not something the validator can observe.

Stdlib only. No network, subprocess, environment reads or filesystem extraction.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
import zlib
from datetime import datetime

POLICY_SCHEMA = 'candidate-artifact-journal-policy/v1'
SNAPSHOT_SCHEMA = 'candidate-artifact-journal-snapshot/v1'
JOURNAL_SCHEMA = 'candidate-artifact-journal/v1'
STAGE = 'artifact-journal-v1-offline-diagnostic'

SHA = re.compile(r'[0-9a-f]{40}')
HEX256 = re.compile(r'[0-9a-f]{64}')
OPERATION_ID = re.compile(r'[0-9a-f]{32}')
REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
BRANCH = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}')
WORKFLOW_PATH = re.compile(r'\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml')
CLOSURE_PATH = re.compile(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*')
ARTIFACT_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}')
JOURNAL_ENTRY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.json')
REASON = re.compile(r'[A-Za-z0-9][A-Za-z0-9 _:./-]{0,79}')

PRODUCER_EVENTS = ('schedule', 'workflow_dispatch')
OPERATIONS = ('label-post', 'comment-post', 'workflow-dispatch')
STATES = ('reserved', 'attempted', 'confirmed', 'rejected', 'unknown')
TRANSITIONS = {None: ('reserved',), 'reserved': ('attempted',),
               'attempted': ('confirmed', 'rejected', 'unknown'),
               'confirmed': (), 'rejected': (), 'unknown': ()}
# Most severe first. 'unknown' is a terminal-uncertain POST outcome.
SEMANTIC = (('unknown', 'unknown-outcome'), ('attempted', 'attempted'), ('reserved', 'reserved'),
            ('confirmed', 'confirmed'), ('rejected', 'rejected'))
# Latest states that leave an operation unresolved for its PR, whatever the candidate identity.
UNRESOLVED = ('reserved', 'attempted', 'unknown')
BLOCKING = ('unknown-outcome', 'attempted', 'reserved')
MAX_RUN_ATTEMPT = 1
MAX_COMPRESSION_RATIO = 32
MAX_ARCHIVE_CEILING = 8 * 1024 * 1024
MAX_RECORDS_CEILING = 10000
# Fixed container-nesting bound for journal JSON. The root object counts as depth 1 and
# every nested object or array adds 1; a document with more than MAX_JSON_DEPTH nested
# containers on any path rejects. Enforced by an iterative walk so the bound does not
# depend on the interpreter's recursion limit.
MAX_JSON_DEPTH = 64
ENCRYPTION_FLAGS = 0x1 | 0x40
SYMLINK_MODE = 0o120000

CANDIDATE_KEYS = ('repo', 'pr', 'head', 'base', 'merge', 'tree')
POLICY_KEYS = ('schema', 'repository_id', 'repository', 'default_branch', 'policy_commit',
               'workflow_id', 'workflow_path', 'producer_events', 'dispatch_actor_ids',
               'source_closure', 'artifact_name', 'journal_entry', 'bounds')
BOUNDS_KEYS = ('max_archive_bytes', 'max_entry_bytes', 'max_records')
SNAPSHOT_KEYS = ('schema', 'observed_at', 'complete', 'default_branch_head', 'run',
                 'workflow_runs', 'artifacts')
RUN_KEYS = ('id', 'run_attempt', 'event', 'status', 'conclusion', 'head_sha', 'head_branch',
            'workflow_id', 'path', 'repository', 'head_repository', 'actor', 'triggering_actor',
            'created_at', 'updated_at')
ACTOR_KEYS = ('id', 'login', 'type')
ARTIFACT_KEYS = ('id', 'name', 'size_in_bytes', 'expired', 'created_at', 'expires_at',
                 'updated_at', 'digest', 'workflow_run')
ARTIFACT_RUN_KEYS = ('id', 'repository_id', 'head_repository_id', 'head_branch', 'head_sha')
JOURNAL_KEYS = ('schema', 'repository_id', 'repository', 'policy_commit', 'workflow_id',
                'workflow_path', 'producer', 'journal_sequence', 'predecessor_digest', 'records')
PRODUCER_KEYS = ('run_id', 'run_attempt', 'event', 'runtime_sha', 'workflow_ref')
RECORD_KEYS = ('sequence', 'operation_id', 'candidate', 'operation', 'state', 'run_id')

DISABLED = {'stage': STAGE, 'action': 'artifact-journal-disabled', 'admitted': 'false',
            'dispatch_allowed': 'false', 'side_effects': 'rejected',
            'reason': 'v1 preparation has no activation route; bootstrap is owner-held'}


class EvidenceError(ValueError):
    """Evidence cannot be used; the caller must fail closed."""


class IntegrityError(EvidenceError):
    """Archive bytes, server digest, ZIP structure or JSON text."""


class ProvenanceError(EvidenceError):
    """Trusted policy versus authenticated run/artifact metadata, or missing evidence."""


class JournalError(EvidenceError):
    """Envelope binding, record transitions, predecessor chain or candidate."""


def _require(condition, error, message):
    if not condition:
        raise error(message)


def _positive_int(value):
    return type(value) is int and value > 0


def _is(value, expected):
    # bool is an int subclass: True == 1. Identity comparisons must match type too.
    return type(value) is type(expected) and value == expected


def _sha(value):
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def _match(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _repository(value):
    # owner/name; neither segment may be exactly '.' or '..' (the pattern alone permits '../..').
    return _match(REPOSITORY, value) and not any(seg in ('.', '..') for seg in value.split('/'))


def _keys(value, keys, error, what):
    _require(isinstance(value, dict) and sorted(value) == sorted(keys), error,
             what + ': exact field set required')
    return value


def _timestamp(value, error, what):
    _require(isinstance(value, str) and 20 <= len(value) <= 35, error, what + ': timestamp missing')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise error(what + ': malformed timestamp') from exc
    _require(parsed.tzinfo is not None, error, what + ': unqualified timestamp')
    return parsed


# --- canonical form and candidate binding ---------------------------------

def canonical_bytes(document):
    return json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def canonical_digest(document):
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def validate_candidate(value):
    _keys(value, CANDIDATE_KEYS, JournalError, 'candidate')
    _require(_repository(value['repo']) and _positive_int(value['pr']), JournalError,
             'candidate: malformed repository/PR')
    _require(all(_sha(value[k]) for k in ('head', 'base', 'merge', 'tree')), JournalError,
             'candidate: full lowercase 40-hex SHAs required')
    return value


def candidate_key(value):
    # Neutral journal identity; runtime gate keys may bind additional fields.
    return canonical_digest(validate_candidate(value))


# --- layer 2: independently supplied trusted policy ------------------------

def validate_policy(policy):
    P = ProvenanceError
    _keys(policy, POLICY_KEYS, P, 'trusted policy')
    _require(policy['schema'] == POLICY_SCHEMA, P, 'trusted policy: unsupported schema')
    _require(_positive_int(policy['repository_id']) and _repository(policy['repository']), P,
             'trusted policy: repository identity')
    branch = policy['default_branch']
    _require(_match(BRANCH, branch) and '..' not in branch and '//' not in branch
             and not branch.endswith(('/', '.lock')), P, 'trusted policy: default branch name')
    _require(_sha(policy['policy_commit']), P, 'trusted policy: policy commit must be a full SHA')
    _require(_positive_int(policy['workflow_id']) and _match(WORKFLOW_PATH, policy['workflow_path']), P,
             'trusted policy: workflow numeric ID and bare workflow path required')
    events = policy['producer_events']
    _require(isinstance(events, list) and events and len(set(events)) == len(events)
             and all(e in PRODUCER_EVENTS for e in events), P,
             'trusted policy: producer events must be schedule and/or workflow_dispatch')
    actors = policy['dispatch_actor_ids']
    _require(isinstance(actors, list) and all(_positive_int(a) for a in actors)
             and len(set(actors)) == len(actors), P, 'trusted policy: dispatch actor IDs')
    closure = _keys(policy['source_closure'], ('commit', 'files'), P, 'trusted policy source closure')
    _require(closure['commit'] == policy['policy_commit'], P,
             'trusted policy: source closure not pinned to the policy commit')
    files = closure['files']
    _require(isinstance(files, dict) and files and policy['workflow_path'] in files, P,
             'trusted policy: source closure must pin the workflow file')
    for path, digest in files.items():
        _require(_match(CLOSURE_PATH, path) and not any(seg in ('.', '..') for seg in path.split('/'))
                 and _match(HEX256, digest), P, 'trusted policy: closure path or digest malformed')
    _require(_match(ARTIFACT_NAME, policy['artifact_name']) and _match(JOURNAL_ENTRY, policy['journal_entry']),
             P, 'trusted policy: artifact name or journal entry name')
    bounds = _keys(policy['bounds'], BOUNDS_KEYS, P, 'trusted policy bounds')
    _require(all(_positive_int(bounds[k]) for k in BOUNDS_KEYS)
             and bounds['max_entry_bytes'] <= bounds['max_archive_bytes'] <= MAX_ARCHIVE_CEILING
             and bounds['max_records'] <= MAX_RECORDS_CEILING, P, 'trusted policy: bounds out of range')
    return policy


def workflow_ref(policy):
    """Qualified form, as GITHUB_WORKFLOW_REF spells it; REST ``path`` stays bare."""
    return policy['repository'] + '/' + policy['workflow_path'] + '@refs/heads/' + policy['default_branch']


# --- layer 2: authenticated snapshot (caller-obtained, caller-trusted) -----

def _actor(value, what):
    _keys(value, ACTOR_KEYS, ProvenanceError, what)
    _require(_positive_int(value['id']) and isinstance(value['login'], str) and value['login']
             and value['type'] in ('User', 'Bot', 'Organization'), ProvenanceError, what + ': malformed actor')
    return value


def validate_run(run, policy):
    """REST workflow run projected to RUN_KEYS. ``head_sha`` here is the default-branch
    policy commit for schedule/dispatch runs; it is not a candidate merge SHA."""
    P = ProvenanceError
    _keys(run, RUN_KEYS, P, 'workflow run')
    _require(_positive_int(run['id']), P, 'workflow run: numeric run ID')
    _require(type(run['run_attempt']) is int and 1 <= run['run_attempt'] <= MAX_RUN_ATTEMPT, P,
             'workflow run: rerun attempts are unsupported in v1')
    _require(run['event'] in policy['producer_events'], P,
             'workflow run: event is not a trusted producer event')
    _require(run['status'] == 'completed' and run['conclusion'] == 'success', P,
             'workflow run: not a successful completed run')
    _require(run['head_sha'] == policy['policy_commit'] and run['head_branch'] == policy['default_branch'], P,
             'workflow run: head is not the current default-branch policy commit')
    _require(_is(run['workflow_id'], policy['workflow_id']) and run['path'] == policy['workflow_path'], P,
             'workflow run: workflow ID/path mismatch')
    repo = _keys(run['repository'], ('id', 'full_name'), P, 'workflow run repository')
    _require(_is(repo['id'], policy['repository_id']) and repo['full_name'] == policy['repository'], P,
             'workflow run: repository identity mismatch')
    head_repo = _keys(run['head_repository'], ('id',), P, 'workflow run head repository')
    _require(_is(head_repo['id'], policy['repository_id']), P,
             'workflow run: head repository is not the policy repository')
    _actor(run['actor'], 'workflow run actor')
    trigger = _actor(run['triggering_actor'], 'workflow run triggering actor')
    if run['event'] == 'workflow_dispatch':
        _require(trigger['type'] == 'User' and trigger['id'] in policy['dispatch_actor_ids'], P,
                 'workflow run: dispatch actor is not owner-reviewed')
    created = _timestamp(run['created_at'], P, 'workflow run')
    updated = _timestamp(run['updated_at'], P, 'workflow run')
    _require(created <= updated, P, 'workflow run: timestamps out of order')
    return run


def validate_artifact(meta, run, policy, observed_at):
    P = ProvenanceError
    _keys(meta, ARTIFACT_KEYS, P, 'artifact')
    _require(_positive_int(meta['id']) and isinstance(meta['name'], str), P, 'artifact: identity')
    _require(_positive_int(meta['size_in_bytes'])
             and meta['size_in_bytes'] <= policy['bounds']['max_archive_bytes'], P,
             'artifact: size out of bounds')
    _require(meta['expired'] is False, P, 'artifact: expired')
    created = _timestamp(meta['created_at'], P, 'artifact')
    updated = _timestamp(meta['updated_at'], P, 'artifact')
    expires = _timestamp(meta['expires_at'], P, 'artifact')
    _require(created <= updated and expires > observed_at, P,
             'artifact: expired or inconsistent timestamps')
    digest = meta['digest']
    _require(isinstance(digest, str) and digest.startswith('sha256:') and _match(HEX256, digest[7:]), P,
             'artifact: server sha256 digest required')
    link = _keys(meta['workflow_run'], ARTIFACT_RUN_KEYS, P, 'artifact workflow_run')
    _require(_is(link['id'], run['id']) and _is(link['repository_id'], policy['repository_id'])
             and _is(link['head_repository_id'], policy['repository_id'])
             and link['head_branch'] == policy['default_branch']
             and link['head_sha'] == policy['policy_commit'], P,
             'artifact: workflow_run binding mismatch')
    return meta


def validate_snapshot(snapshot, policy):
    """One complete bounded observation. Returns (run, journal artifact metadata).

    Unavailable evidence (HTTP 403/404/410, rate limit, transport uncertainty) is a
    ProvenanceError, never an empty or permissive state.
    """
    P = ProvenanceError
    _require(snapshot is not None, P, 'evidence unavailable: no authenticated snapshot')
    if isinstance(snapshot, dict) and 'unavailable' in snapshot:
        reason = snapshot['unavailable']
        raise P('evidence unavailable: ' + (reason if _match(REASON, reason) else 'unspecified'))
    _keys(snapshot, SNAPSHOT_KEYS, P, 'snapshot')
    _require(snapshot['schema'] == SNAPSHOT_SCHEMA, P, 'snapshot: unsupported schema')
    observed = _timestamp(snapshot['observed_at'], P, 'snapshot')
    _require(snapshot['complete'] is True, P, 'snapshot: incomplete bounded listing')
    _require(snapshot['default_branch_head'] == policy['policy_commit'], P,
             'snapshot: default branch moved from the policy commit')
    run = validate_run(snapshot['run'], policy)
    listing = snapshot['workflow_runs']
    _require(isinstance(listing, list) and listing, P, 'snapshot: workflow run listing missing')
    seen = set()
    for entry in listing:
        _keys(entry, RUN_KEYS, P, 'workflow run listing entry')
        _require(_positive_int(entry['id']) and entry['id'] not in seen, P,
                 'snapshot: duplicate or malformed listing identity')
        seen.add(entry['id'])
        _require(_is(entry['workflow_id'], policy['workflow_id']) and entry['path'] == policy['workflow_path'],
                 P, 'snapshot: listing contains a foreign workflow')
        _require(entry['event'] in PRODUCER_EVENTS and entry['head_branch'] == policy['default_branch'], P,
                 'snapshot: workflow accepted a candidate-controlled event or branch')
        _require(entry['id'] <= run['id'], P,
                 'snapshot: a newer run exists; the selected run is not authoritative')
        if entry['id'] == run['id']:
            _require(entry == run, P, 'snapshot: listing entry differs from the selected run')
    _require(run['id'] in seen, P, 'snapshot: selected run absent from the complete listing')
    artifacts = snapshot['artifacts']
    _require(isinstance(artifacts, list), P, 'snapshot: artifact listing missing')
    ids, matches = set(), []
    for meta in artifacts:
        _keys(meta, ARTIFACT_KEYS, P, 'artifact')
        _require(_positive_int(meta['id']) and meta['id'] not in ids, P, 'snapshot: duplicate artifact identity')
        ids.add(meta['id'])
        if meta['name'] == policy['artifact_name']:
            matches.append(meta)
    _require(len(matches) == 1, P, 'snapshot: journal artifact missing, deleted or ambiguous')
    return run, validate_artifact(matches[0], run, policy, observed)


def _snapshot_body(snapshot):
    return {k: v for k, v in snapshot.items() if k != 'observed_at'}


# --- layer 1: artifact integrity ------------------------------------------

def parse_strict_json(data, max_bytes):
    _require(isinstance(data, (bytes, bytearray)) and len(data) <= max_bytes, IntegrityError,
             'journal entry exceeds bound')

    def pairs(items):
        keys = [k for k, _ in items]
        _require(len(keys) == len(set(keys)), IntegrityError, 'duplicate JSON object key')
        return dict(items)

    def constant(_name):
        raise IntegrityError('non-finite JSON number')

    try:
        value = json.loads(bytes(data).decode('utf-8'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, IntegrityError):
            raise
        raise IntegrityError('malformed journal JSON') from exc
    _require(isinstance(value, dict), IntegrityError, 'journal entry is not a JSON object')
    _check_json_depth(value)
    return value


def _check_json_depth(root):
    """Reject container nesting deeper than MAX_JSON_DEPTH (root object is depth 1).

    Iterative, so the check itself never recurses; json.loads may or may not have
    raised RecursionError first, depending on the interpreter, and this makes the
    outcome identical either way.
    """
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        _require(depth <= MAX_JSON_DEPTH, IntegrityError, 'journal JSON nesting exceeds bound')
        children = node.values() if isinstance(node, dict) else node
        for child in children:
            if isinstance(child, (dict, list)):
                stack.append((child, depth + 1))


def verify_archive(archive, artifact, policy):
    """Digest, size and bounded single-entry ZIP checks; returns the parsed JSON object.

    Never extracts to the filesystem. ``artifact`` is the authenticated metadata
    already validated by ``validate_snapshot``.
    """
    integrity_error = IntegrityError
    bounds = policy['bounds']
    _require(isinstance(archive, (bytes, bytearray)), integrity_error, 'archive: bytes required')
    archive = bytes(archive)
    _require(len(archive) == artifact['size_in_bytes'] and len(archive) <= bounds['max_archive_bytes'], integrity_error,
             'archive: downloaded size differs from authenticated size_in_bytes')
    _require('sha256:' + hashlib.sha256(archive).hexdigest() == artifact['digest'], integrity_error,
             'archive: sha256 does not match the authenticated artifact digest')
    try:
        container = zipfile.ZipFile(io.BytesIO(archive))
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError) as exc:
        raise integrity_error('archive: not a readable ZIP') from exc
    with container:
        _require(container.comment == b'', integrity_error, 'archive: unexpected archive comment')
        infos = container.infolist()
        _require(len(infos) == 1, integrity_error, 'archive: exactly one entry required (no duplicates or extras)')
        info = infos[0]
        _require(info.filename == policy['journal_entry'] and not info.is_dir(), integrity_error,
                 'archive: entry name is not the expected journal entry')
        _require(info.flag_bits & ENCRYPTION_FLAGS == 0, integrity_error, 'archive: encrypted entry')
        _require((info.external_attr >> 16) & 0o170000 != SYMLINK_MODE, integrity_error, 'archive: symlink entry')
        _require(info.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED), integrity_error,
                 'archive: unsupported compression method')
        _require(0 < info.file_size <= bounds['max_entry_bytes'], integrity_error, 'archive: entry size out of bounds')
        _require(info.compress_size <= len(archive), integrity_error, 'archive: compressed size exceeds archive')
        # The ratio is measured against the actual compressed size with no floor, so a
        # small entry cannot expand beyond MAX_COMPRESSION_RATIO either. A nonempty entry
        # that claims zero compressed bytes is malformed, not infinitely compressible.
        _require(info.compress_size > 0, integrity_error, 'archive: zero compressed size for a nonempty entry')
        _require(info.file_size <= MAX_COMPRESSION_RATIO * info.compress_size, integrity_error,
                 'archive: suspicious compression ratio')
        try:
            # testzip only translates BadZipFile (CRC/structure); invalid raw deflate bytes
            # surface from the decompressor as zlib.error and must fail closed the same way.
            _require(container.testzip() is None, integrity_error, 'archive: CRC or structure check failed')
            with container.open(info) as handle:
                data = handle.read(bounds['max_entry_bytes'] + 1)
        except (zipfile.BadZipFile, zlib.error, RuntimeError, EOFError, OSError, ValueError) as exc:
            if isinstance(exc, IntegrityError):
                raise
            raise integrity_error('archive: entry could not be read') from exc
    _require(len(data) == info.file_size, integrity_error, 'archive: entry length differs from header')
    return parse_strict_json(data, bounds['max_entry_bytes'])


# --- layer 3: semantic journal --------------------------------------------

def validate_records(records, repository, max_records, producer_run_id):
    J = JournalError
    _require(isinstance(records, list) and len(records) <= max_records, J, 'records: not a bounded list')
    last, bound = {}, {}
    for index, record in enumerate(records, start=1):
        _keys(record, RECORD_KEYS, J, 'record')
        _require(_is(record['sequence'], index), J, 'records: sequence must be contiguous and ordered')
        _require(_match(OPERATION_ID, record['operation_id']), J, 'record: operation ID')
        candidate = validate_candidate(record['candidate'])
        _require(candidate['repo'] == repository, J,
                 'record: candidate repository differs from the journal repository')
        _require(record['operation'] in OPERATIONS and record['state'] in STATES, J,
                 'record: unknown operation or state')
        _require(_positive_int(record['run_id']) and record['run_id'] <= producer_run_id, J,
                 'record: recording run newer than the producer run')
        op = record['operation_id']
        previous = last.get(op)
        _require(record['state'] in TRANSITIONS[previous], J, 'record: illegal state transition')
        if previous is None:
            bound[op] = (candidate, record['operation'])
        else:
            _require(bound[op] == (candidate, record['operation']), J,
                     'record: operation rebound to a different candidate or operation')
        last[op] = record['state']
    return records


def validate_journal(document, policy, run):
    """Envelope must bind (equal) the trusted policy and the authenticated run.
    It cannot assert them: a mismatch is a JournalError, not a policy update."""
    J = JournalError
    _keys(document, JOURNAL_KEYS, J, 'journal envelope')
    _require(document['schema'] == JOURNAL_SCHEMA, J, 'journal envelope: unsupported schema')
    for field in ('repository_id', 'repository', 'policy_commit', 'workflow_id', 'workflow_path'):
        _require(_is(document[field], policy[field]), J,
                 'journal envelope: ' + field + ' does not bind the trusted policy')
    producer = _keys(document['producer'], PRODUCER_KEYS, J, 'journal producer')
    _require(_is(producer['run_id'], run['id']) and _is(producer['run_attempt'], run['run_attempt'])
             and producer['run_attempt'] == 1, J,
             'journal producer: run ID/attempt does not bind the authenticated run')
    _require(producer['event'] == run['event'], J, 'journal producer: event mismatch')
    _require(producer['runtime_sha'] == run['head_sha'] == policy['policy_commit'], J,
             'journal producer: runtime GITHUB_SHA must equal the default-branch policy commit')
    _require(producer['workflow_ref'] == workflow_ref(policy), J,
             'journal producer: qualified workflow ref mismatch')
    sequence = document['journal_sequence']
    _require(_positive_int(sequence), J, 'journal envelope: sequence')
    predecessor = document['predecessor_digest']
    _require((predecessor is None) == (sequence == 1) and (predecessor is None or _match(HEX256, predecessor)),
             J, 'journal envelope: predecessor digest must be null exactly for sequence 1')
    validate_records(document['records'], policy['repository'], policy['bounds']['max_records'],
                     producer['run_id'])
    return document


def reconcile(previous, current):
    """Append-only chain check between a validated previous evidence state and the
    validated current journal. Returns 'genesis', 'unchanged' or 'extended'.

    ``previous`` is one of
      {'kind': 'genesis'}                       caller-supplied offline bootstrap context
      {'kind': 'journal', 'document': {...}}    the last validated journal
      {'kind': 'unavailable', 'reason': ...}    lost/expired/deleted/uncertain evidence
    Lost evidence never becomes a fresh genesis; it is operator reconciliation.

    Genesis is context the caller asserts; this code cannot authenticate a bootstrap.
    In v1 only the offline test harness supplies it. Real bootstrap and operator
    acceptance of a genesis remain owner-held activation prerequisites.
    """
    J = JournalError
    _require(isinstance(previous, dict) and isinstance(previous.get('kind'), str), J,
             'previous evidence: kind required')
    kind = previous['kind']
    if kind == 'unavailable':
        raise J('previous journal evidence lost, expired, deleted or uncertain: '
                'operator reconciliation required; no fresh genesis')
    if kind == 'genesis':
        _keys(previous, ('kind',), J, 'previous evidence')
        _require(current['journal_sequence'] == 1 and current['predecessor_digest'] is None, J,
                 'explicit genesis requires journal sequence 1 with no predecessor')
        return 'genesis'
    _require(kind == 'journal', J, 'previous evidence: unsupported kind')
    _keys(previous, ('kind', 'document'), J, 'previous evidence')
    prev = previous['document']
    _require(isinstance(prev, dict) and isinstance(prev.get('records'), list)
             and _positive_int(prev.get('journal_sequence')), J, 'previous evidence: malformed journal document')
    if current == prev:
        return 'unchanged'
    _require(_is(current['repository_id'], prev.get('repository_id'))
             and current['repository'] == prev.get('repository'), J,
             'journal identity changed across reconciliation')
    _require(current['journal_sequence'] == prev['journal_sequence'] + 1, J,
             'journal sequence rewound, repeated with changes, or skipped')
    _require(current['predecessor_digest'] == canonical_digest(prev), J,
             'predecessor digest does not chain to the previous journal')
    count = len(prev['records'])
    _require(len(current['records']) >= count and current['records'][:count] == prev['records'], J,
             'previous record dropped or rewritten; unresolved and terminal history must be preserved')
    return 'extended'


def journal_status(evidence, candidate):
    """Semantic state for one candidate plus a PR-level unresolved scan. Not an authorization.

    ``evidence`` is a validated journal document, or None / {'unavailable': ...}.
    Missing or uncertain evidence is 'evidence-unavailable' and never 'not-dispatched'.

    ``candidate_state`` reduces only records bound to the exact candidate identity
    (repo, pr, head, base, merge, tree). ``pr_unresolved_operations`` lists every
    operation for the same repo/PR, under any candidate identity, whose latest state
    is reserved, attempted or unknown. A PR-level label/comment POST with an unknown
    outcome is not undone by a head, base, merge or tree movement, so when such
    operations exist under another identity and the candidate's own state is not
    already blocking, ``semantic_state`` is the explicit 'pr-unresolved' and operator
    reconciliation is required. A moved head never reduces to 'not-dispatched' while
    its PR has unresolved operations.
    """
    validate_candidate(candidate)
    if evidence is None or (isinstance(evidence, dict) and 'unavailable' in evidence):
        return {'semantic_state': 'evidence-unavailable', 'candidate_state': 'evidence-unavailable',
                'operator_reconciliation_required': True, 'in_flight': False, 'operations': [],
                'pr_unresolved_operations': []}
    _require(isinstance(evidence, dict) and isinstance(evidence.get('records'), list), JournalError,
             'journal status: validated journal document required')
    last, pr_last = {}, {}
    for record in evidence['records']:
        _require(isinstance(record, dict) and all(k in record for k in ('candidate', 'operation_id', 'state')),
                 JournalError, 'journal status: malformed record')
        recorded = record['candidate']
        if recorded == candidate:
            last[record['operation_id']] = record['state']
        if (isinstance(recorded, dict) and recorded.get('repo') == candidate['repo']
                and _is(recorded.get('pr'), candidate['pr'])):
            pr_last[record['operation_id']] = record['state']
    states = set(last.values())
    candidate_state = next((name for state, name in SEMANTIC if state in states), 'not-dispatched')
    pr_unresolved = sorted(op for op, state in pr_last.items() if state in UNRESOLVED)
    # Operation IDs bind one candidate, so an unresolved op absent from ``last`` belongs
    # to another identity of the same PR (moved head/base/merge/tree).
    other_identity_unresolved = any(op not in last for op in pr_unresolved)
    semantic = candidate_state
    if other_identity_unresolved and candidate_state not in BLOCKING:
        semantic = 'pr-unresolved'
    return {'semantic_state': semantic,
            'candidate_state': candidate_state,
            'operator_reconciliation_required': candidate_state == 'unknown-outcome' or other_identity_unresolved,
            'in_flight': candidate_state in ('reserved', 'attempted'),
            'operations': sorted(last),
            'pr_unresolved_operations': pr_unresolved}


# --- layer 4: side-effect authority (always rejected in v1) ----------------

def stage(**diagnostic):
    """Result envelope. The stage label and authority fields are set last and cannot
    be overridden by any diagnostic key (including ``stage=...``)."""
    result = dict(diagnostic)
    result['stage'] = STAGE
    result['admitted'] = False
    result['dispatch_allowed'] = False
    result['side_effects'] = 'rejected'
    return result


def evaluate_offline(trusted_policy, snapshot_before, snapshot_after, archive, candidate, previous_evidence):
    """Offline diagnostic over caller-supplied inputs. Raises EvidenceError on any
    defect; otherwise returns a stage with diagnostic_valid=True and no authority."""
    policy = validate_policy(trusted_policy)
    validate_candidate(candidate)
    _require(candidate['repo'] == policy['repository'], JournalError,
             'candidate repository differs from the policy repository')
    run, artifact = validate_snapshot(snapshot_before, policy)
    validate_snapshot(snapshot_after, policy)
    _require(_snapshot_body(snapshot_before) == _snapshot_body(snapshot_after)
             and _timestamp(snapshot_after['observed_at'], ProvenanceError, 'snapshot')
             >= _timestamp(snapshot_before['observed_at'], ProvenanceError, 'snapshot'),
             ProvenanceError, 'snapshot moved between the before and after observations')
    document = verify_archive(archive, artifact, policy)
    validate_journal(document, policy, run)
    chain = reconcile(previous_evidence, document)
    status = journal_status(document, candidate)
    return stage(diagnostic_valid=True, candidate_key=candidate_key(candidate), run_id=run['id'],
                 artifact_id=artifact['id'], artifact_digest=artifact['digest'],
                 journal_digest=canonical_digest(document), journal_sequence=document['journal_sequence'],
                 chain=chain, **status)


def disabled_entry(*_ignored, **_also_ignored):
    """Production entry point: rejects every side effect.

    Positional and keyword inputs (payload, environment, configuration, callbacks)
    are accepted only so an accidental caller receives the rejection instead of a
    TypeError. They are never read and no callback is ever invoked.
    """
    return dict(DISABLED)


def main(argv=None, environ=None):
    result = disabled_entry(argv, environ)
    print('::notice::' + result['action'] + ': ' + result['reason'])
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
