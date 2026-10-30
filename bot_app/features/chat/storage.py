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
            CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,scope TEXT,batch TEXT,state TEXT,content TEXT,created REAL,message_id TEXT,autonomous INTEGER,UNIQUE(scope,batch));''')
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

    def recent(self, scope_prefix):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM decisions WHERE scope LIKE ? ORDER BY id DESC LIMIT 100', (scope_prefix + '%',))]

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

    def retire(self):
        with self.db() as db:
            db.execute("UPDATE deliveries SET state='cancelled',content='' WHERE state='prepared'")
            db.execute("UPDATE deliveries SET state='unknown' WHERE state='sending'")

    def redact(self, scope):
        with self.db() as db:
            db.execute("UPDATE deliveries SET content='' WHERE scope=?", (scope,))
            db.execute("DELETE FROM decisions WHERE scope=?", (scope,))
