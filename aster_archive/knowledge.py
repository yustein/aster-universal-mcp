"""Versioned local extractive knowledge, never an authority to execute history.

Summaries quote a bounded, disclosed selection of the selected branch. Decision
labels are conservative retrieval aids, not verification of intent or acceptance.
Every result stays linked to exact canonical passages and is scoped at read time.
"""
from __future__ import annotations

import argparse
import base64
from bisect import bisect_right
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys

from psycopg.types.json import Jsonb

from .access import ArchiveScope
from .store import PostgresArchive, ArchiveBusy, ArchiveNotFound, _identity, _scope_sql, stable_id
from .storage import minimum_free_bytes, require_storage_reserve

PIPELINE_KEY = 'extractive-quotes-and-decision-candidates-v1'
MAX_REVISION_PASSAGES = 10000
MAX_REVISION_NODES = 10000
MAX_ATTEMPTS = 3
MAX_DERIVED_RECORDS = 5000
KINDS = ('summary', 'decision_candidate', 'cognee_summary')
_HEX = re.compile(r'[a-f0-9]{64}\Z')
_MARKER = re.compile(
    r"\b(?:decid(?:e|ed)|approv(?:e|ed)|agree(?:d)?|let[’']s|we should|I want|"
    r"I recommend|I propose|recommendation|proposal|instead|reject(?:ed)?)\b|"
    r'\b(?:karar|onaylıyorum|kabul ediyorum|öneriyorum|önerim)\b', re.I)
_APPROVAL = re.compile(r'^(?:I approve\b|I accept\b|I agree to\b|Approved[.:! ]|'
                       r'Onaylıyorum\b|Kabul ediyorum\b)', re.I)
_SELECTION = re.compile(r'^(?:I (?:have )?decided\b|We (?:have )?decided\b|'
                        r"Let[’']s (?:use|build|keep|choose|separate|do|run)\b|"
                        r'I want\b|Karar(?:ım| verdim)\b)', re.I)


class KnowledgeBudgetError(ValueError):
    pass


def _timestamp(node: dict) -> float | None:
    raw = node.get('raw') or {}
    message = raw.get('message') or {}
    value = message.get('create_time')
    return float(value) if type(value) in (int, float) and math.isfinite(value) else None


def _citation(row: dict, start: int, end: int) -> dict:
    return {key: row[key] for key in ('passage_id','revision_id','node_id','part_index','role')} | {
        'start_char': row['start_char'] + start,
        'end_char': row['start_char'] + end,
        'passage_start_char': row['start_char'],
        'passage_end_char': row['end_char'],
        'exact_quote': row['text'][start:end],
        'offset_unit': 'Unicode characters in original content part',
    }


def _spans(text: str):
    """Sentence/line spans; reject quoted/code lines as approval evidence.

    Sentence extraction is a heuristic. Exact spans and candidate labels are
    retained so a caller can inspect the full surrounding original message.
    """
    fenced = False
    position = 0
    for line in text.splitlines(keepends=True):
        clean = line.lstrip()
        if clean.startswith(('```', '~~~')):
            fenced = not fenced
        elif not fenced and not clean.startswith(('>', '"', '“', "'")):
            for match in re.finditer(r'[^.!?\n]+[.!?]?', line):
                value = match.group()
                left = len(value) - len(value.lstrip())
                right = len(value.rstrip())
                if right > left:
                    yield position + match.start() + left, position + match.start() + right
        position += len(line)


def _decision_label(role: str, quote: str) -> str:
    if role != 'user':
        return 'assistant_suggestion_or_statement'
    if '?' not in quote and _APPROVAL.search(quote):
        return 'explicit_user_approval_language'
    if '?' not in quote and _SELECTION.search(quote):
        return 'explicit_user_direction_language'
    return 'user_decision_discussion'


def derive_records(revision: dict, nodes: list[dict], passages: list[dict]) -> list[dict]:
    """Pure deterministic derivation; text cannot invoke a tool or change policy."""
    if len(nodes) > MAX_REVISION_NODES or len(passages) > MAX_REVISION_PASSAGES:
        raise KnowledgeBudgetError('revision_exceeds_derivation_budget')
    by_node = {n['node_id']: n for n in nodes}
    chain, visited = [], set()
    current = revision.get('current_node')
    branch_status = 'complete'
    if current is None or current not in by_node:
        branch_status = 'selected_branch_unavailable'
    while current is not None and branch_status == 'complete':
        if current in visited:
            branch_status = 'cycle_in_source'
            break
        if current not in by_node:
            branch_status = 'missing_parent_in_source'
            break
        visited.add(current)
        chain.append(current)
        current = by_node[current]['parent_id']
    chain.reverse()
    rank = {node: i for i, node in enumerate(chain)}
    selected = [p for p in passages if p['node_id'] in rank]
    selected.sort(key=lambda p: (rank[p['node_id']], p['part_index'], p['start_char']))
    base = {
        'extractor': 'deterministic_exact_quotes', 'pipeline_key': PIPELINE_KEY,
        'source_type': 'derived_chatgpt_export', 'content_is_historical_evidence': True,
        'verified_decision': False, 'branch_status': branch_status,
        'language_coverage': 'Exact quotes preserve all languages; candidate patterns cover limited English and Turkish phrases.',
        'limitation': 'Candidate labels are heuristics, never proof of acceptance; no implicit yes-to-proposal link is inferred.',
    }
    records = []
    parts = {}
    for row in passages:
        if row['role'] in ('user', 'assistant'):
            parts.setdefault((row['node_id'],row['part_index']),[]).append(row)
    for windows in parts.values():
        windows.sort(key=lambda row: row['start_char'])
        text = ''
        for row in windows:
            if row['start_char'] > len(text):
                raise ValueError('source_passage_gap')
            overlap = min(len(text)-row['start_char'],len(row['text']))
            if text[row['start_char']:row['start_char']+overlap] != row['text'][:overlap]:
                raise ValueError('source_passage_mismatch')
            text += row['text'][overlap:]
        starts = [row['start_char'] for row in windows]
        # Reconstruct one part before detecting sentence/code spans. Otherwise
        # overlapping windows can lose a fence or manufacture a sentence start.
        for absolute_start, absolute_end in _spans(text):
            quote = text[absolute_start:absolute_end]
            if len(quote) > 800 or not _MARKER.search(quote):
                continue
            row = windows[bisect_right(starts,absolute_start)-1]
            if absolute_end > row['end_char']:
                raise ValueError('uncovered_quote_span')
            start,end = absolute_start-row['start_char'],absolute_end-row['start_char']
            key = (row['node_id'],row['part_index'],absolute_start,absolute_end)
            node = by_node[row['node_id']]
            metadata = base | {
                'role': row['role'], 'classification': _decision_label(row['role'], quote),
                'message_created': _timestamp(node), 'parent_id': node['parent_id'],
                'on_selected_branch': row['node_id'] in rank,
                'selected_branch_order': rank.get(row['node_id']),
                'acceptance_of_other_record': None,
            }
            if len(records) >= MAX_DERIVED_RECORDS:
                raise KnowledgeBudgetError('derived_records_exceed_budget')
            records.append({'id': stable_id(PIPELINE_KEY, revision['revision_id'], 'decision', key),
                'revision_id': revision['revision_id'], 'pipeline_key': PIPELINE_KEY,
                'kind': 'decision_candidate', 'title': revision.get('title') or 'Untitled conversation',
                'text': quote, 'metadata': metadata, 'citations': [_citation(row, start, end)]})
    # One quote per message. Include first/latest user and assistant messages,
    # then evenly spaced messages; disclose precisely how much was selected.
    firsts = {}
    for row in selected:
        firsts.setdefault(row['node_id'], row)
    values = list(firsts.values())
    indexes = {0, len(values) - 1} if values else set()
    for role in ('user', 'assistant'):
        positions = [i for i, row in enumerate(values) if row['role'] == role]
        if positions:
            indexes.update((positions[0], positions[-1]))
    if len(values) > 1:
        indexes.update(round(i * (len(values) - 1) / 7) for i in range(8))
    quotes = []
    for index in sorted(indexes):
        row = values[index]
        end = min(len(row['text']), 400)
        quotes.append(_citation(row, 0, end))
    summary_text = '\n\n'.join(f"{q['role']}: {q['exact_quote']}" for q in quotes)
    if not quotes:
        summary_text = 'No extractive summary: the selected branch contains no accessible text passages.'
    records.append({'id': stable_id(PIPELINE_KEY, revision['revision_id'], 'summary'),
        'revision_id': revision['revision_id'], 'pipeline_key': PIPELINE_KEY,
        'kind': 'summary', 'title': revision.get('title') or 'Untitled conversation',
        'text': summary_text, 'citations': quotes, 'metadata': base | {
            'summary_method': 'Selected source excerpts, not a model-written summary.',
            'selection': 'First/latest messages by role and evenly spaced selected-branch messages; first 400 characters per selected message.',
            'selected_message_count': len(quotes), 'selected_branch_message_count': len(values),
            'source_passage_count': len(passages), 'source_node_count': len(nodes),
            'omitted_message_count': len(values) - len(quotes),
            'partial_message_quotes': True,
        }})
    return records


_RECORD_SELECT = '''SELECT k.*,r.owner_id,r.source_id,r.conversation_id,r.source_updated,
    c.project_id,c.current_revision,c.current_ambiguous
    FROM aster_archive.knowledge_records k
    JOIN aster_archive.conversation_revisions r USING(revision_id)
    JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)'''


def _result(row: dict) -> dict:
    metadata = dict(row['metadata']) | {
        key: row[key] for key in ('revision_id','source_id','conversation_id','project_id','source_updated')}
    metadata.update(is_current_revision=row['revision_id'] == row['current_revision'],
                    current_revision_ambiguous=row['current_ambiguous'])
    citations = [dict(c, source_id=row['source_id'], conversation_id=row['conversation_id']) for c in row['citations']]
    return {'id': row['record_id'], 'title': row['title'], 'text': row['text'],
            'kind': row['kind'], 'metadata': metadata, 'citations': citations}


class KnowledgeStore:
    def __init__(self, store: PostgresArchive):
        self.store = store

    def migrate(self):
        from .enrichment import migrate_component
        migrate_component(self.store, 'knowledge', 1, 'knowledge_schema.sql')

    def write_records(self, conn, records):
        for row in records:
            conn.execute('''INSERT INTO aster_archive.knowledge_records
                (record_id,revision_id,pipeline_key,kind,title,text,metadata,citations)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(record_id) DO NOTHING''',
                (row['id'],row['revision_id'],row['pipeline_key'],row['kind'],row['title'],
                 row['text'],Jsonb(row['metadata']),Jsonb(row['citations'])))

    def fetch(self, scope: ArchiveScope, record_id: str) -> dict:
        if not isinstance(record_id, str) or not _HEX.fullmatch(record_id):
            raise ValueError('invalid knowledge record identity')
        where, args = _scope_sql(scope)
        with self.store.connection(readonly=True) as conn:
            row = conn.execute(_RECORD_SELECT + f' WHERE {where} AND k.record_id=%s', args + [record_id]).fetchone()
            if row is None:
                raise ArchiveNotFound('knowledge record unavailable')
            return _result(row)

    def search(self, scope: ArchiveScope, query: str, kind: str | None = None, limit: int = 20) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 1000 or '\x00' in query:
            raise ValueError('invalid knowledge query')
        if type(limit) is not int or not 1 <= limit <= 20 or kind is not None and kind not in KINDS:
            raise ValueError('invalid knowledge filter')
        where, args = _scope_sql(scope)
        extra = ''
        if kind is not None:
            extra = ' AND k.kind=%s'
            args.append(kind)
        with self.store.connection(readonly=True) as conn:
            rows = conn.execute(_RECORD_SELECT + f''' WHERE {where} {extra}
                AND c.current_revision=k.revision_id AND (
                  k.lexemes_simple @@ websearch_to_tsquery('simple',%s) OR
                  k.lexemes_english @@ websearch_to_tsquery('english',%s) OR
                  k.lexemes_turkish @@ websearch_to_tsquery('turkish',%s))
                ORDER BY r.source_updated DESC NULLS LAST,k.record_id LIMIT %s''',
                args + [query,query,query,limit]).fetchall()
        return {'results': [_result(row) for row in rows], 'basis': 'derived_records_current_revisions'}

    def history(self, scope: ArchiveScope, conversation_id: str, limit=20, cursor=None) -> dict:
        _identity(conversation_id)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError('invalid history limit')
        scope_key = stable_id(scope.owner_id, scope.source_ids, scope.project_ids, conversation_id)
        after = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError()
                data = json.loads(base64.urlsafe_b64decode(cursor))
                if (data['scope'] != scope_key or data['v'] != 1
                        or not _HEX.fullmatch(data['after']) or not _HEX.fullmatch(data['revision'])
                        or type(data['time']) not in (int,float) or not math.isfinite(data['time'])):
                    raise ValueError()
                after = (data['time'],data['revision'],data['after'])
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise ValueError('invalid history cursor') from None
        where, args = _scope_sql(scope)
        moment = "COALESCE((k.metadata->>'message_created')::double precision,r.source_updated,1e300::double precision)"
        extra = ''
        args += [conversation_id]
        if after is not None:
            extra = f' AND ({moment},k.revision_id,k.record_id)>(%s,%s,%s)'
            args += list(after)
        query = _RECORD_SELECT.replace('SELECT k.*',f'SELECT {moment} AS sort_time,k.*')
        with self.store.connection(readonly=True) as conn:
            rows = conn.execute(query + f" WHERE {where} AND r.conversation_id=%s"
                + " AND k.kind='decision_candidate'" + extra
                + f' ORDER BY {moment},k.revision_id,k.record_id LIMIT %s',args+[limit+1]).fetchall()
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit-1]
            next_cursor = base64.urlsafe_b64encode(json.dumps({'v':1,'scope':scope_key,
                'after':last['record_id'],'revision':last['revision_id'],'time':last['sort_time']}).encode()).decode()
        return {'records': [_result(row) for row in rows[:limit]], 'next_cursor': next_cursor,
                'order': 'message creation time, otherwise source revision update time; unknown times last; identity breaks ties',
                'scope': 'retained revisions and alternative branches; candidates are not a verified decision ledger'}

    def coverage(self, scope: ArchiveScope) -> dict:
        where, args = _scope_sql(scope)
        with self.store.connection(readonly=True) as conn:
            records = conn.execute(f'''SELECT k.kind,k.pipeline_key,count(*) AS records
                FROM aster_archive.knowledge_records k
                JOIN aster_archive.conversation_revisions r USING(revision_id)
                JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
                WHERE {where} AND c.current_revision=k.revision_id GROUP BY k.kind,k.pipeline_key''',args).fetchall()
            jobs = conn.execute(f'''SELECT j.pipeline_key,j.status,j.error_code,count(*) AS revisions
                FROM aster_archive.knowledge_jobs j
                JOIN aster_archive.conversation_revisions r USING(revision_id)
                JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
                WHERE {where} AND c.current_revision=j.revision_id GROUP BY j.pipeline_key,j.status,j.error_code''',args).fetchall()
        return {'records':records,'jobs':jobs,'verified_decisions':0,'automatic_acceptance_inference':False}


def derive_pending(store: PostgresArchive, owner_id: str, source_id: str, archive_root: Path, limit: int = 10) -> dict:
    """One bounded batch. Session lock is lost on crash, so running jobs resume.

    The row attempt starts before work; records and completion commit together.
    Three crashes/failures exhaust the budget and retain a failed receipt.
    """
    _identity(owner_id); _identity(source_id)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid derivation batch limit')
    minimum_free_bytes()
    knowledge = KnowledgeStore(store)
    # Separate lock key: canonical importer can continue while derivations run.
    with store.import_lock(owner_id, 'knowledge:' + stable_id(source_id, PIPELINE_KEY)) as conn:
        conn.execute('''INSERT INTO aster_archive.knowledge_jobs(revision_id,pipeline_key)
            SELECT r.revision_id,%s FROM aster_archive.conversation_revisions r
            JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
            WHERE c.owner_id=%s AND c.source_id=%s AND c.tombstoned_at IS NULL
            ON CONFLICT DO NOTHING''', (PIPELINE_KEY,owner_id,source_id))
        conn.execute('''UPDATE aster_archive.knowledge_jobs j SET status='failed',error_code='attempts_exhausted'
            FROM aster_archive.conversation_revisions r WHERE j.revision_id=r.revision_id
            AND r.owner_id=%s AND r.source_id=%s AND j.pipeline_key=%s
            AND j.status='running' AND j.attempts>=%s''',(owner_id,source_id,PIPELINE_KEY,MAX_ATTEMPTS))
        jobs = conn.execute('''SELECT r.* FROM aster_archive.knowledge_jobs j
            JOIN aster_archive.conversation_revisions r USING(revision_id)
            JOIN aster_archive.conversations c USING(owner_id,source_id,conversation_id)
            WHERE c.owner_id=%s AND c.source_id=%s AND c.tombstoned_at IS NULL
            AND j.pipeline_key=%s AND j.status IN ('pending','running') AND j.attempts<%s
            ORDER BY (r.revision_id=c.current_revision) DESC,r.source_updated DESC NULLS LAST,r.revision_id LIMIT %s''',
            (owner_id,source_id,PIPELINE_KEY,MAX_ATTEMPTS,limit)).fetchall()
        done, failed = 0, 0
        for revision in jobs:
            # Capacity/configuration is checked before consuming an attempt.
            require_storage_reserve(Path(archive_root))
            rid = revision['revision_id']
            conn.execute('''UPDATE aster_archive.knowledge_jobs SET status='running',attempts=attempts+1,
                started_at=now(),error_code=NULL WHERE revision_id=%s AND pipeline_key=%s''',(rid,PIPELINE_KEY))
            try:
                nodes = conn.execute("SELECT node_id,parent_id,jsonb_build_object('message',jsonb_build_object('create_time',raw->'message'->'create_time')) AS raw FROM aster_archive.nodes WHERE revision_id=%s ORDER BY node_id LIMIT %s",
                                     (rid,MAX_REVISION_NODES+1)).fetchall()
                passages = conn.execute('''SELECT p.*,n.role FROM aster_archive.passages p
                    JOIN aster_archive.nodes n USING(revision_id,node_id)
                    WHERE p.revision_id=%s ORDER BY p.node_id,p.part_index,p.start_char LIMIT %s''',
                    (rid,MAX_REVISION_PASSAGES+1)).fetchall()
                records = derive_records(revision,nodes,passages)
                with conn.transaction():
                    # A tombstone committed during extraction must hide results
                    # before they can be committed/exposed.
                    alive = conn.execute('''SELECT 1 FROM aster_archive.conversations
                        WHERE owner_id=%s AND source_id=%s AND conversation_id=%s AND tombstoned_at IS NULL''',
                        (owner_id,source_id,revision['conversation_id'])).fetchone()
                    if alive:
                        knowledge.write_records(conn,records)
                    conn.execute('''UPDATE aster_archive.knowledge_jobs SET status='complete',finished_at=now()
                        WHERE revision_id=%s AND pipeline_key=%s''',(rid,PIPELINE_KEY))
                    conn.execute("UPDATE aster_archive.derivations SET state='complete',pipeline_key=%s WHERE revision_id=%s AND kind='knowledge'",(PIPELINE_KEY,rid))
                done += 1
            except (ValueError, LookupError):
                conn.execute('''UPDATE aster_archive.knowledge_jobs SET status='failed',finished_at=now(),error_code='derivation_budget_or_source_error'
                    WHERE revision_id=%s AND pipeline_key=%s''',(rid,PIPELINE_KEY))
                failed += 1
        return {'completed':done,'failed':failed,'pipeline_key':PIPELINE_KEY}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('migrate','derive','status','search','history'))
    parser.add_argument('--owner'); parser.add_argument('--source'); parser.add_argument('--archive-root',type=Path)
    parser.add_argument('--limit',type=int,default=10); parser.add_argument('--query'); parser.add_argument('--conversation')
    args = parser.parse_args(argv)
    store = PostgresArchive(os.environ.get('ASTER_ARCHIVE_DATABASE_URL',''))
    knowledge = KnowledgeStore(store)
    if args.action == 'migrate':
        knowledge.migrate(); result = {'knowledge_schema':'ready'}
    else:
        _identity(args.owner); _identity(args.source)
        scope = ArchiveScope('local-operator',args.owner,(args.source,),None)
        if args.action == 'derive':
            result = derive_pending(store,args.owner,args.source,args.archive_root,limit=args.limit)
        elif args.action == 'status':
            result = knowledge.coverage(scope)
        elif args.action == 'search':
            result = knowledge.search(scope,args.query,limit=args.limit)
        else:
            result = knowledge.history(scope,args.conversation,limit=args.limit)
    print(json.dumps(result,ensure_ascii=False,default=str))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        print(json.dumps({'error':'KNOWLEDGE_OPERATION_FAILED'}),file=sys.stderr)
        sys.exit(1)
