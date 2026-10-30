"""SQLite source of truth. All calls run on the memory worker, not the bot loop."""
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(version INTEGER NOT NULL);
INSERT INTO meta SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM meta);
CREATE TABLE IF NOT EXISTS messages(
 scope TEXT, id TEXT, author TEXT, name TEXT, content TEXT, created REAL,
 revision INTEGER DEFAULT 1, deleted INTEGER DEFAULT 0, bot INTEGER DEFAULT 0,
 processed INTEGER DEFAULT 0, observed INTEGER DEFAULT 0, PRIMARY KEY(scope,id));
CREATE INDEX IF NOT EXISTS pending_messages ON messages(scope,processed,created);
CREATE TABLE IF NOT EXISTS memories(
 id TEXT PRIMARY KEY, scope TEXT, subject TEXT, kind TEXT, slot TEXT,
 content TEXT, state TEXT, created REAL, updated REAL, valid_until REAL,
 supersedes TEXT, attribution TEXT, fingerprint TEXT);
CREATE INDEX IF NOT EXISTS memory_scope ON memories(scope,state,subject);
CREATE TABLE IF NOT EXISTS evidence(
 memory_id TEXT REFERENCES memories(id) ON DELETE CASCADE,
 scope TEXT, message_id TEXT, revision INTEGER, PRIMARY KEY(memory_id,message_id));
CREATE TABLE IF NOT EXISTS vectors(
 memory_id TEXT PRIMARY KEY REFERENCES memories(id) ON DELETE CASCADE,
 model TEXT, dimensions INTEGER, vector TEXT);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, scope TEXT, payload TEXT, state TEXT, attempts INTEGER DEFAULT 0,
 available REAL, lease REAL DEFAULT 0, error TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS epochs(scope TEXT PRIMARY KEY, version INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, scope TEXT, operation TEXT, target TEXT, created REAL);
CREATE TABLE IF NOT EXISTS forgotten(scope TEXT, author TEXT, cutoff REAL, PRIMARY KEY(scope,author));
CREATE TABLE IF NOT EXISTS deleted_messages(scope TEXT, message_id TEXT, PRIMARY KEY(scope,message_id));
"""

def terms(text):
    """Latin words and CJK bigrams; SQLite's default tokenizer misses Chinese."""
    latin = re.findall(r"[a-z0-9_]{2,}", text.casefold())
    chinese = re.findall(r"[\u3400-\u9fff]+", text)
    return set(latin + [word[i:i+2] for word in chinese for i in range(max(1, len(word)-1))])

class MemoryRepository:
    def __init__(self, path):
        self.path = Path(path)
        self.initialized = False

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            if not self.initialized:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(SCHEMA)
                if db.execute("SELECT version FROM meta").fetchone()[0] != 1:
                    raise ValueError("Unsupported memory database version; restore a compatible backup.")
                self.initialized = True
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def bump(db, scope):
        db.execute("INSERT INTO epochs VALUES(?,1) ON CONFLICT(scope) DO UPDATE SET version=version+1", (scope,))

    def epoch(self, scope):
        with self.db() as db:
            row = db.execute("SELECT version FROM epochs WHERE scope=?", (scope,)).fetchone()
            return row[0] if row else 0

    def ingest(self, event, observed=False):
        scope = event.scope.key
        with self.db() as db:
            if db.execute('SELECT 1 FROM deleted_messages WHERE scope=? AND message_id=?', (scope, event.message_id)).fetchone():
                return False
            forgotten = db.execute("SELECT cutoff FROM forgotten WHERE scope=? AND author=?", (scope, event.author_id)).fetchone()
            if forgotten and event.created_at <= forgotten[0]:
                return False
            old = db.execute("SELECT content,deleted FROM messages WHERE scope=? AND id=?", (scope, event.message_id)).fetchone()
            if old and (old["deleted"] or old["content"] == event.content[:4000]):
                return False
            if old:
                db.execute("UPDATE messages SET content=?,name=?,revision=revision+1,processed=0 WHERE scope=? AND id=?", (event.content[:4000], event.author_name[:100], scope, event.message_id))
                self._invalidate(db, scope, event.message_id)
                self.bump(db, scope)
            else:
                db.execute("INSERT INTO messages(scope,id,author,name,content,created,bot,processed,observed) VALUES(?,?,?,?,?,?,?,?,?)",
                           (scope, event.message_id, event.author_id, event.author_name[:100], event.content[:4000], event.created_at, int(event.is_bot), int(event.is_bot), int(observed)))
            return True

    def recent(self, scope, limit=50):
        with self.db() as db:
            rows = db.execute("SELECT * FROM messages WHERE scope=? AND deleted=0 AND content<>'' ORDER BY created DESC LIMIT ?", (scope, min(100, limit))).fetchall()
            return [dict(row) for row in reversed(rows)]

    @staticmethod
    def _invalidate(db, scope, message_id):
        MemoryRepository._redact_jobs(db, scope, {message_id})
        ids = [r[0] for r in db.execute("SELECT memory_id FROM evidence WHERE scope=? AND message_id=?", (scope, message_id))]
        for memory_id in ids:
            db.execute("UPDATE memories SET state='deleted',content='',updated=? WHERE id=?", (time.time(), memory_id))
            db.execute("DELETE FROM vectors WHERE memory_id=?", (memory_id,))

    @staticmethod
    def _redact_jobs(db, scope, message_ids):
        """Retire complete batches: an in-flight copy is rejected by source revisions."""
        for row in db.execute("SELECT id,payload FROM jobs WHERE scope=? AND payload<>'[]'", (scope,)).fetchall():
            if any(item['id'] in message_ids for item in json.loads(row['payload'])):
                db.execute("UPDATE jobs SET state='obsolete',payload='[]',error='' WHERE id=?", (row['id'],))

    def delete_message(self, scope, message_id):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO deleted_messages VALUES(?,?)', (scope, message_id))
            db.execute("UPDATE messages SET deleted=1,content='',revision=revision+1,processed=1 WHERE scope=? AND id=?", (scope, message_id))
            self._invalidate(db, scope, message_id)
            self.bump(db, scope)

    def queue(self, scope, *, batch_size, wait_seconds, force=False):
        now = time.time()
        with self.db() as db:
            if db.execute("SELECT 1 FROM jobs WHERE scope=? AND state IN ('queued','running')", (scope,)).fetchone():
                return False
            rows = db.execute("SELECT * FROM messages WHERE scope=? AND processed=0 AND deleted=0 AND bot=0 ORDER BY created LIMIT 50", (scope,)).fetchall()
            if not rows or (not force and len(rows) < batch_size and now - rows[0]["created"] < wait_seconds):
                return False
            payload = [dict(row) for row in rows]
            fingerprint = hashlib.sha256(json.dumps([(r['id'], r['revision']) for r in payload]).encode()).hexdigest()
            job_id = hashlib.sha256((scope + fingerprint).encode()).hexdigest()
            db.execute("INSERT OR IGNORE INTO jobs(id,scope,payload,state,available) VALUES(?,?,?,'queued',?)", (job_id, scope, json.dumps(payload, ensure_ascii=False), now))
            return True

    def claim(self, scopes=None):
        now = time.time()
        with self.db() as db:
            db.execute("UPDATE jobs SET state='queued' WHERE state='running' AND lease<?", (now,))
            query, args = "SELECT * FROM jobs WHERE state='queued' AND available<=?", [now]
            if scopes is not None:
                if not scopes:
                    return None
                query += ' AND scope IN (' + ','.join('?' for _ in scopes) + ')'
                args.extend(scopes)
            row = db.execute(query + ' ORDER BY available LIMIT 1', args).fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',attempts=attempts+1,lease=? WHERE id=?", (now + 120, row['id']))
            return dict(row, payload=json.loads(row['payload']), attempts=row['attempts'] + 1)

    def fail(self, job, reason):
        with self.db() as db:
            failed = job['attempts'] >= 3
            db.execute("UPDATE jobs SET state=?,available=?,error=? WHERE id=? AND state='running' AND payload<>'[]'", ('failed' if failed else 'queued', time.time() + 30 * job['attempts'], reason[:200], job['id']))
            if failed:
                for row in job['payload']:
                    db.execute("UPDATE messages SET processed=2 WHERE scope=? AND id=? AND revision=?", (job['scope'], row['id'], row['revision']))
                db.execute("UPDATE jobs SET payload='[]' WHERE id=?", (job['id'],))

    def obsolete(self, job, excluded_ids):
        with self.db() as db:
            db.execute("UPDATE jobs SET state='obsolete',payload='[]' WHERE id=?", (job['id'],))
            for message_id in excluded_ids:
                db.execute("UPDATE messages SET processed=2 WHERE scope=? AND id=?", (job['scope'], message_id))

    def apply(self, job, items, summary=""):
        """Validate evidence and attribution inside the same write transaction."""
        scope, created = job['scope'], []
        with self.db() as db:
            source = {}
            for message in job['payload']:
                row = db.execute("SELECT * FROM messages WHERE scope=? AND id=? AND revision=? AND deleted=0 AND content<>''", (scope, message['id'], message['revision'])).fetchone()
                if row:
                    source[row['id']] = row
            if len(source) != len(job['payload']):
                db.execute("UPDATE jobs SET state='obsolete',payload='[]' WHERE id=?", (job['id'],))
                return []
            candidates = list(items[:20])
            if summary:
                candidates.append({"kind": "summary", "content": summary, "source_ids": list(source), "subject_id": "", "attribution": "summary"})
            for item in candidates:
                kind, text = item.get('kind'), item.get('content')
                ids = item.get('source_ids', [])
                subject = str(item.get('subject_id') or '')
                attribution = item.get('attribution', 'uncertain')
                if kind not in {'fact', 'episode', 'summary'} or not isinstance(text, str) or not 1 <= len(text.strip()) <= 1800:
                    continue
                if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in source for i in ids):
                    continue
                if kind == 'fact' and (not subject or not any(source[i]['author'] == subject for i in ids)):
                    continue
                observed = any(source[i]['observed'] for i in ids)
                active = not observed and attribution in {'self_report', 'confirmed_event', 'summary'}
                state = 'active' if active else 'candidate'
                fingerprint = hashlib.sha256((scope + subject + kind + text.strip().casefold()).encode()).hexdigest()
                existing = db.execute("SELECT id FROM memories WHERE fingerprint=? AND state IN ('active','candidate')", (fingerprint,)).fetchone()
                if existing:
                    memory_id = existing[0]
                else:
                    memory_id = uuid.uuid4().hex
                    slot = item.get('slot', '')
                    slot = slot[:100] if isinstance(slot, str) else ''
                    supersedes = None
                    # Only explicit exclusive corrections supersede facts; preferences can coexist.
                    if active and kind in {'fact', 'episode'} and item.get('correction') is True and slot:
                        old = db.execute("SELECT id FROM memories WHERE scope=? AND subject=? AND kind=? AND slot=? AND state='active'", (scope, subject, kind, slot)).fetchall()
                        for previous in old:
                            db.execute("UPDATE memories SET state='superseded',updated=? WHERE id=?", (time.time(), previous[0]))
                            db.execute("UPDATE memories SET state='superseded' WHERE scope=? AND kind='summary' AND id IN (SELECT s.memory_id FROM evidence s JOIN evidence old ON s.scope=old.scope AND s.message_id=old.message_id WHERE old.memory_id=?)", (scope, previous[0]))
                            supersedes = previous[0]
                        if old:
                            self.bump(db, scope)
                    until = item.get('valid_until')
                    until = float(until) if type(until) in {int, float} and math.isfinite(until) and until > 0 else None
                    db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (memory_id, scope, subject, kind, slot, text.strip(), state, time.time(), time.time(), until, supersedes, attribution, fingerprint))
                    created.append(memory_id)
                for source_id in ids:
                    db.execute("INSERT OR IGNORE INTO evidence VALUES(?,?,?,?)", (memory_id, scope, source_id, source[source_id]['revision']))
            for row in source.values():
                db.execute("UPDATE messages SET processed=1 WHERE scope=? AND id=? AND revision=?", (scope, row['id'], row['revision']))
            db.execute("UPDATE jobs SET state='done',payload='[]',error='' WHERE id=?", (job['id'],))
        return created

    def list(self, scope, *, subject=None, limit=100, offset=0, candidates=False, history=False):
        with self.db() as db:
            query = "SELECT * FROM memories WHERE scope=? AND state IN ('active','candidate')" if candidates else "SELECT * FROM memories WHERE scope=? AND state='active'"
            args = [scope]
            if history:
                query = "SELECT * FROM memories WHERE scope=?"
            else:
                query += " AND (valid_until IS NULL OR valid_until>?)"
                args.append(time.time())
            if subject is not None:
                query += " AND subject=?"
                args.append(str(subject))
            rows = [dict(row) for row in db.execute(query + " ORDER BY updated DESC LIMIT ? OFFSET ?", (*args, min(1000, limit), offset))]
            for row in rows:
                row['sources'] = [dict(source) for source in db.execute("SELECT e.message_id,m.author,m.created,m.name,m.content FROM evidence e JOIN messages m ON m.scope=e.scope AND m.id=e.message_id WHERE e.memory_id=? AND m.deleted=0 AND m.revision=e.revision", (row['id'],))]
            return rows

    def search(self, scope, query, *, subject=None, limit=6, embedding=None):
        rows = self.list(scope, limit=1000)
        query_terms = terms(query)
        scored = []
        with self.db() as db:
            for row in rows:
                lexical = len(query_terms & terms(row['content'])) / max(1, len(query_terms))
                semantic = 0
                if embedding:
                    vector = db.execute("SELECT vector FROM vectors WHERE memory_id=? AND model=? AND dimensions=?", (row['id'], embedding[0], len(embedding[1]))).fetchone()
                    if vector:
                        a, b = json.loads(vector[0]), embedding[1]
                        denom = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
                        semantic = sum(x*y for x,y in zip(a,b)) / denom if denom else 0
                profile = row['kind'] == 'fact' and row['subject'] == str(subject)
                score = max(lexical, semantic) + (0.15 if profile else 0)
                if score > 0.1:
                    scored.append((score, row))
        return [row for _, row in sorted(scored, key=lambda pair: pair[0], reverse=True)[:limit]]

    def pending_vectors(self, scope):
        return [row for row in self.list(scope, limit=100) if row['kind'] != 'summary']

    def unindexed(self, scope, model):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT m.id,m.content FROM memories m LEFT JOIN vectors v ON v.memory_id=m.id AND v.model=? WHERE m.scope=? AND m.state='active' AND v.memory_id IS NULL ORDER BY m.updated DESC LIMIT 2", (model, scope))]

    def vector(self, memory_id, embedding):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO vectors SELECT id,?,?,? FROM memories WHERE id=? AND state='active'", (embedding[0], len(embedding[1]), json.dumps(embedding[1]), memory_id))

    def forget(self, scope, *, actor=None, memory_id=None):
        with self.db() as db:
            if memory_id:
                row = db.execute("SELECT subject FROM memories WHERE scope=? AND id=? AND state<>'deleted'", (scope, memory_id)).fetchone()
                if row is None:
                    return 0
                if actor is not None and row[0] != str(actor):
                    raise ValueError("You may only delete your own profile memories.")
                sources = db.execute('SELECT e.message_id,m.author FROM evidence e JOIN messages m ON m.scope=e.scope AND m.id=e.message_id WHERE e.memory_id=?', (memory_id,)).fetchall()
                for source in sources:
                    if actor is None or source['author'] == str(actor):
                        self._invalidate(db, scope, source['message_id'])
                        db.execute('INSERT OR IGNORE INTO deleted_messages VALUES(?,?)', (scope, source['message_id']))
                        db.execute("UPDATE messages SET content='',deleted=1,revision=revision+1,processed=1 WHERE scope=? AND id=?", (scope, source['message_id']))
                db.execute("UPDATE memories SET state='deleted',content='',updated=? WHERE scope=? AND id=?", (time.time(), scope, memory_id))
                db.execute("DELETE FROM vectors WHERE memory_id=?", (memory_id,))
                count = 1
            else:
                if actor is None:
                    raise ValueError("A member is required for scoped forgetting.")
                messages = db.execute("SELECT id FROM messages WHERE scope=? AND author=?", (scope, str(actor))).fetchall()
                for message in messages:
                    self._invalidate(db, scope, message[0])
                db.execute("UPDATE messages SET content='',deleted=1,revision=revision+1,processed=1 WHERE scope=? AND author=?", (scope, str(actor)))
                cursor = db.execute("UPDATE memories SET state='deleted',content='',updated=? WHERE scope=? AND subject=?", (time.time(), scope, str(actor)))
                db.execute("DELETE FROM vectors WHERE memory_id IN (SELECT id FROM memories WHERE scope=? AND state='deleted')", (scope,))
                db.execute("INSERT INTO forgotten VALUES(?,?,?) ON CONFLICT(scope,author) DO UPDATE SET cutoff=excluded.cutoff", (scope, str(actor), time.time()))
                count = cursor.rowcount
            self.bump(db, scope)
            db.execute("INSERT INTO audit(scope,operation,target,created) VALUES(?,?,?,?)", (scope, 'forget', memory_id or str(actor), time.time()))
            return count

    def correct(self, scope, memory_id, text, *, actor=None):
        from bot_app.shared.redaction import contains_secret
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1800:
            raise ValueError("Correction must be 1-1800 characters.")
        if contains_secret(text):
            raise ValueError('Credential-like content cannot be stored as memory.')
        with self.db() as db:
            old = db.execute("SELECT * FROM memories WHERE scope=? AND id=? AND state IN ('active','candidate')", (scope, memory_id)).fetchone()
            if not old or (actor is not None and old['subject'] != str(actor)):
                raise ValueError("Memory not found in your allowed scope.")
            new_id = uuid.uuid4().hex
            db.execute("UPDATE memories SET state='superseded' WHERE id=?", (memory_id,))
            db.execute("UPDATE memories SET state='superseded' WHERE scope=? AND kind='summary' AND id IN (SELECT s.memory_id FROM evidence s JOIN evidence old ON s.scope=old.scope AND s.message_id=old.message_id WHERE old.memory_id=?)", (scope, memory_id))
            fingerprint = hashlib.sha256((scope + text + new_id).encode()).hexdigest()
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (new_id, scope, old['subject'], old['kind'], old['slot'], text.strip(), 'active', time.time(), time.time(), None, memory_id, 'member_correction' if actor else 'admin_correction', fingerprint))
            db.execute("INSERT INTO evidence SELECT ?,scope,message_id,revision FROM evidence WHERE memory_id=?", (new_id, memory_id))
            self.bump(db, scope)
            db.execute("INSERT INTO audit(scope,operation,target,created) VALUES(?,?,?,?)", (scope, 'correct', memory_id, time.time()))
            return new_id

    def cleanup(self, scope, retention_days):
        with self.db() as db:
            cutoff = time.time() - retention_days * 86400
            ids = {row[0] for row in db.execute("SELECT id FROM messages WHERE scope=? AND created<? AND content<>''", (scope, cutoff))}
            self._redact_jobs(db, scope, ids)
            db.execute("UPDATE messages SET content='',processed=2 WHERE scope=? AND created<?", (scope, cutoff))
            db.execute("UPDATE memories SET state='expired' WHERE scope=? AND state IN ('active','candidate') AND valid_until IS NOT NULL AND valid_until<?", (scope, time.time()))
            db.execute("DELETE FROM vectors WHERE memory_id IN (SELECT id FROM memories WHERE scope=? AND state IN ('deleted','expired'))", (scope,))

    def stats(self, scope_prefix):
        with self.db() as db:
            return {"memories": [dict(r) for r in db.execute("SELECT state,kind,count(*) count FROM memories WHERE scope LIKE ? GROUP BY state,kind", (scope_prefix + '%',))],
                    "jobs": [dict(r) for r in db.execute("SELECT state,count(*) count FROM jobs WHERE scope LIKE ? GROUP BY state", (scope_prefix + '%',))]}

    def pending_scopes(self):
        with self.db() as db:
            return [row[0] for row in db.execute("SELECT DISTINCT scope FROM messages WHERE processed=0 AND deleted=0 LIMIT 100")]

    def all_scopes(self):
        with self.db() as db:
            return [row[0] for row in db.execute("SELECT DISTINCT scope FROM messages")]

    def approve(self, scope, memory_id):
        with self.db() as db:
            invalid = db.execute("SELECT 1 FROM evidence e JOIN messages m ON m.scope=e.scope AND m.id=e.message_id WHERE e.memory_id=? AND (m.deleted=1 OR m.revision<>e.revision)", (memory_id,)).fetchone()
            if invalid:
                raise ValueError("Source changed; this candidate cannot be approved.")
            cursor = db.execute("UPDATE memories SET state='active',updated=? WHERE scope=? AND id=? AND state='candidate'", (time.time(), scope, memory_id))
            self.bump(db, scope)
            return cursor.rowcount
