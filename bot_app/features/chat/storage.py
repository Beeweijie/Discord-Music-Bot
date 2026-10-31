"""Durable decisions and outgoing-message idempotency, scoped by channel."""
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

class ChatRepository:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.executescript('''CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY,scope TEXT,action TEXT,reason TEXT,created REAL,detail TEXT);
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,scope TEXT,batch TEXT,state TEXT,content TEXT,created REAL,message_id TEXT,autonomous INTEGER,UNIQUE(scope,batch));
            CREATE TABLE IF NOT EXISTS delivery_parts(delivery_id TEXT,part INTEGER,state TEXT,content TEXT,message_id TEXT,PRIMARY KEY(delivery_id,part));''')
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, scope, action, reason, detail=None):
        with self.db() as db:
            db.execute('INSERT INTO decisions(scope,action,reason,created,detail) VALUES(?,?,?,?,?)', (scope, action, reason[:100], time.time(), json.dumps(detail or {}, ensure_ascii=False)))
            db.execute('DELETE FROM decisions WHERE id NOT IN (SELECT id FROM decisions ORDER BY id DESC LIMIT 5000)')
            db.execute('DELETE FROM deliveries WHERE created<?', (time.time() - 30 * 86400,))
            db.execute('DELETE FROM delivery_parts WHERE delivery_id NOT IN (SELECT id FROM deliveries)')

    def recent(self, scope_prefix):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM decisions WHERE scope LIKE ? ORDER BY id DESC LIMIT 100', (scope_prefix + '%',))]

    def sources(self,scope):
        """A small, channel-scoped citation trail; no queries or fetched bodies."""
        from .web_presentation import result_sources
        sources=[]
        with self.db() as db:
            rows=db.execute("SELECT detail,created FROM decisions WHERE scope=? AND action='tool' "
                "AND reason IN ('web_search','read_webpage') AND created>? ORDER BY id DESC LIMIT 6",
                (scope,time.time()-1800)).fetchall()
            for row in rows:
                detail=json.loads(row['detail'])
                if detail.get('status')!='completed':
                    continue
                for source in result_sources({'results':detail.get('sources',[])}):
                    if source['url'] not in {item['url'] for item in sources}:
                        sources.append(dict(source,retrieved_at=row['created']))
                    if len(sources)>=3:
                        return sources
        return sources

    def allowed(self, scope, hour, day):
        with self.db() as db:
            rows = db.execute("SELECT created FROM deliveries WHERE scope=? AND autonomous=1 AND state IN ('prepared','sending','sent','unknown') AND created>?", (scope, time.time() - 86400)).fetchall()
            return (not day or len(rows) < day) and sum(row[0] > time.time() - 3600 for row in rows) < hour

    def reserve(self, scope, batch, content, autonomous):
        with self.db() as db:
            delivery_id = uuid.uuid4().hex
            cursor = db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,?,'prepared',?,?,NULL,?)", (delivery_id, scope, batch, content, time.time(), int(autonomous)))
            return delivery_id if cursor.rowcount else None

    def finish(self, delivery_id, state, message_id=None):
        with self.db() as db:
            db.execute('UPDATE deliveries SET state=?,message_id=? WHERE id=?', (state, str(message_id) if message_id else None, delivery_id))

    def prepare_parts(self,delivery_id,parts):
        with self.db() as db:
            db.executemany("INSERT OR IGNORE INTO delivery_parts VALUES(?,?,'prepared',?,NULL)",
                           [(delivery_id,index,text) for index,text in enumerate(parts)])

    def finish_part(self,delivery_id,index,state,message_id=None):
        with self.db() as db:
            db.execute('UPDATE delivery_parts SET state=?,message_id=? WHERE delivery_id=? AND part=?',
                       (state,str(message_id) if message_id else None,delivery_id,index))

    def retire(self):
        with self.db() as db:
            db.execute("UPDATE deliveries SET state='cancelled',content='' WHERE state='prepared'")
            db.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")
            db.execute("UPDATE delivery_parts SET state='cancelled',content='' WHERE state='prepared'")
            db.execute("UPDATE delivery_parts SET state='unknown' WHERE state='sending'")

    def redact(self, scope):
        with self.db() as db:
            db.execute("UPDATE deliveries SET content='' WHERE scope=?", (scope,))
            db.execute("DELETE FROM decisions WHERE scope=?", (scope,))
            db.execute("UPDATE delivery_parts SET content='' WHERE delivery_id IN (SELECT id FROM deliveries WHERE scope=?)",(scope,))
