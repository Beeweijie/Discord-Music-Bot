"""Channel-scoped learning evidence and single-slot, reversible revisions."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from .policy import messages_per_review, review_interval, learning_phase, question_gap


class PersonaRepository:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.executescript('''
                CREATE TABLE IF NOT EXISTS persona_state(scope TEXT PRIMARY KEY,total INTEGER DEFAULT 0,reviewed INTEGER DEFAULT 0,last_review REAL DEFAULT 0,epoch INTEGER DEFAULT 0,last_question REAL DEFAULT 0,question_total INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS persona_seen(scope TEXT,id TEXT,PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS persona_removed(scope TEXT,id TEXT,PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS persona_forgotten(scope TEXT,author TEXT,cutoff REAL,PRIMARY KEY(scope,author));
                CREATE TABLE IF NOT EXISTS persona_evidence(scope TEXT,id TEXT,author TEXT,kind TEXT,content TEXT,url TEXT,created REAL,PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS persona_notes(scope TEXT,core TEXT,slot TEXT,text TEXT,sources TEXT,PRIMARY KEY(scope,core,slot));
                CREATE TABLE IF NOT EXISTS persona_history(id INTEGER PRIMARY KEY,scope TEXT,core TEXT,slot TEXT,before TEXT,after TEXT,sources TEXT,reason TEXT,created REAL,kind TEXT);
            ''')
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _state(db, scope):
        db.execute('INSERT OR IGNORE INTO persona_state(scope) VALUES(?)', (scope,))
        return dict(db.execute('SELECT * FROM persona_state WHERE scope=?', (scope,)).fetchone())

    def observe(self, scope, identifier, author, kind, content, url='', created=None):
        now = time.time()
        created = now if created is None else created
        with self.db() as db:
            self._state(db, scope)
            if db.execute('SELECT 1 FROM persona_removed WHERE scope=? AND id=?',(scope,identifier)).fetchone():
                return
            forgotten = db.execute('SELECT cutoff FROM persona_forgotten WHERE scope=? AND author=?',(scope,author)).fetchone()
            if forgotten and created<=forgotten['cutoff']:
                return
            fresh = db.execute('INSERT OR IGNORE INTO persona_seen VALUES(?,?)', (scope, identifier)).rowcount
            if fresh and kind == 'chat':
                db.execute('UPDATE persona_state SET total=total+1 WHERE scope=?', (scope,))
            old = db.execute('SELECT content FROM persona_evidence WHERE scope=? AND id=?', (scope,identifier)).fetchone()
            if old and old['content'] != content:
                self._invalidate(db,scope,{identifier})
            db.execute('INSERT OR REPLACE INTO persona_evidence VALUES(?,?,?,?,?,?,?)', (scope,identifier,author,kind,content[:700],url[:2000],created))
            db.execute("UPDATE persona_evidence SET content='',url='' WHERE scope=? AND created<?", (scope,now-30*86400))

    def snapshot(self, scope, core):
        with self.db() as db:
            state = self._state(db,scope)
            notes = [dict(row) for row in db.execute('SELECT slot,text,sources FROM persona_notes WHERE scope=? AND core=? ORDER BY slot',(scope,core))]
            history = [dict(row) for row in db.execute('SELECT id,slot,before,after,sources,reason,created,kind FROM persona_history WHERE scope=? AND core=? ORDER BY id DESC LIMIT 30',(scope,core))]
            return dict(state, notes=notes, history=history, messages_per_review=messages_per_review(state['total']),
                        review_interval_seconds=round(review_interval(state['total'])),phase=learning_phase(state['total']))

    def claim(self, scope, core):
        now = time.time()
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = self._state(db,scope)
            if state['total']-state['reviewed'] < messages_per_review(state['total']) or now-state['last_review'] < review_interval(state['total']):
                return None
            evidence = [dict(row) for row in db.execute("SELECT * FROM persona_evidence WHERE scope=? AND content<>'' ORDER BY created DESC LIMIT 50",(scope,))]
            if sum(row['kind']=='chat' for row in evidence)<2:
                return None
            db.execute('UPDATE persona_state SET reviewed=total,last_review=? WHERE scope=?',(now,scope))
            notes = [dict(row) for row in db.execute('SELECT slot,text,sources FROM persona_notes WHERE scope=? AND core=?',(scope,core))]
            return dict(state,core=core,evidence=evidence,notes=notes)

    def question_allowed(self,scope):
        with self.db() as db:
            state = self._state(db,scope)
            return (state['total']<1200 and state['total']-state['question_total']>=question_gap(state['total'])
                    and time.time()-state['last_question']>=1800)

    def asked(self,scope):
        with self.db() as db:
            self._state(db,scope)
            db.execute('UPDATE persona_state SET last_question=?,question_total=total WHERE scope=?',(time.time(),scope))

    def apply(self, scope, job, change):
        identifiers = change['source_ids']
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._state(db,scope)['epoch']!=job['epoch']:
                return False
            present = {row['id'] for row in db.execute('SELECT id FROM persona_evidence WHERE scope=?',(scope,))}
            if not set(identifiers)<=present:
                return False
            old = db.execute('SELECT text,sources FROM persona_notes WHERE scope=? AND core=? AND slot=?',(scope,job['core'],change['slot'])).fetchone()
            if old and old['text']==change['text']:
                return False
            before = json.dumps(dict(old) if old else None,ensure_ascii=False)
            sources = json.dumps(identifiers)
            db.execute('INSERT OR REPLACE INTO persona_notes VALUES(?,?,?,?,?)',(scope,job['core'],change['slot'],change['text'],sources))
            db.execute('INSERT INTO persona_history(scope,core,slot,before,after,sources,reason,created,kind) VALUES(?,?,?,?,?,?,?,?,?)',
                (scope,job['core'],change['slot'],before,change['text'],sources,change['reason'],time.time(),'learn'))
            db.execute('UPDATE persona_state SET epoch=epoch+1 WHERE scope=?',(scope,))
            return True

    @staticmethod
    def _invalidate(db,scope,identifiers):
        for row in db.execute('SELECT core,slot,sources FROM persona_notes WHERE scope=?',(scope,)).fetchall():
            if set(json.loads(row['sources'])) & identifiers:
                db.execute('DELETE FROM persona_notes WHERE scope=? AND core=? AND slot=?',(scope,row['core'],row['slot']))
        for row in db.execute('SELECT id,sources,before FROM persona_history WHERE scope=?',(scope,)).fetchall():
            before = json.loads(row['before'])
            old_sources = set(json.loads(before['sources'])) if before else set()
            if (set(json.loads(row['sources'])) | old_sources) & identifiers:
                db.execute("UPDATE persona_history SET before='null',after='',sources='[]',reason='Source removed',kind='redacted' WHERE id=?",(row['id'],))
        db.execute('UPDATE persona_state SET epoch=epoch+1 WHERE scope=?',(scope,))

    def forget(self, scope, *, author=None, identifier=None, deleted=True):
        with self.db() as db:
            self._state(db,scope)
            rows = list(db.execute('SELECT id,author FROM persona_evidence WHERE scope=?',(scope,)))
            ids = {row['id'] for row in rows if (author is None or row['author']==author) and (identifier is None or row['id']==identifier)}
            if identifier is not None:
                ids.add(identifier)
            if deleted:
                db.executemany('INSERT OR IGNORE INTO persona_removed VALUES(?,?)',[(scope,item) for item in ids])
            if identifier is None:
                authors = {author} if author else {row['author'] for row in rows if row['author']}
                db.executemany('INSERT OR REPLACE INTO persona_forgotten VALUES(?,?,?)',[(scope,item,time.time()) for item in authors])
            self._invalidate(db,scope,ids)
            db.executemany('DELETE FROM persona_evidence WHERE scope=? AND id=?',[(scope,item) for item in ids])

    def revert(self, scope, core, identifier):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM persona_history WHERE id=? AND scope=? AND core=?',(identifier,scope,core)).fetchone()
            if not row or row['kind']!='learn':
                raise ValueError('Select an available learned revision.')
            latest = db.execute('SELECT id FROM persona_history WHERE scope=? AND core=? AND slot=? ORDER BY id DESC LIMIT 1',(scope,core,row['slot'])).fetchone()
            note = db.execute('SELECT text,sources FROM persona_notes WHERE scope=? AND core=? AND slot=?',(scope,core,row['slot'])).fetchone()
            if not note or note['text']!=row['after'] or latest['id']!=identifier:
                raise ValueError('This slot has changed; reload its history.')
            before = json.loads(row['before'])
            present = {r['id'] for r in db.execute('SELECT id FROM persona_evidence WHERE scope=?',(scope,))}
            if before and not set(json.loads(before['sources']))<=present:
                raise ValueError('Previous sources are no longer available.')
            db.execute('DELETE FROM persona_notes WHERE scope=? AND core=? AND slot=?',(scope,core,row['slot']))
            if before:
                db.execute('INSERT INTO persona_notes VALUES(?,?,?,?,?)',(scope,core,row['slot'],before['text'],before['sources']))
            db.execute('INSERT INTO persona_history(scope,core,slot,before,after,sources,reason,created,kind) VALUES(?,?,?,?,?,?,?,?,?)',
                (scope,core,row['slot'],json.dumps(dict(note)),before['text'] if before else '',before['sources'] if before else '[]','Administrator reverted this change',time.time(),'revert'))
            db.execute('UPDATE persona_state SET epoch=epoch+1 WHERE scope=?',(scope,))
            return True
