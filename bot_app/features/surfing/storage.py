"""Restart-safe instance-wide limits and discovery deduplication; no chat text."""
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from .policy import ATTEMPTS_PER_DAY, SHARES_PER_DAY, ATTEMPT_INTERVAL, MIN_NEW_MESSAGES, canonical_url, title_key


class SurfRepository:
    def __init__(self,path):
        self.path=Path(path)

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=5)
        db.row_factory=sqlite3.Row
        db.executescript('''CREATE TABLE IF NOT EXISTS surf_channels(scope TEXT PRIMARY KEY,total INTEGER DEFAULT 0,reviewed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS surf_seen(scope TEXT,message_id TEXT,PRIMARY KEY(scope,message_id));
            CREATE TABLE IF NOT EXISTS surf_runs(id TEXT PRIMARY KEY,scope TEXT,day TEXT,created REAL,state TEXT,url TEXT DEFAULT '',title TEXT DEFAULT '');''')
        try:
            with db:
                yield db
        finally:
            db.close()

    def observe(self,scope,message_id):
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO surf_channels(scope) VALUES(?)',(scope,))
            if db.execute('INSERT OR IGNORE INTO surf_seen VALUES(?,?)',(scope,message_id)).rowcount:
                db.execute('UPDATE surf_channels SET total=total+1 WHERE scope=?',(scope,))

    def claim(self,scope,day,now=None):
        now=time.time() if now is None else now
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT total,reviewed FROM surf_channels WHERE scope=?',(scope,)).fetchone()
            if not row or row['total']-row['reviewed']<MIN_NEW_MESSAGES:
                return None
            counts=db.execute("SELECT count(*),sum(state IN ('sending','shared','unknown')) FROM surf_runs WHERE day=?",(day,)).fetchone()
            latest=db.execute('SELECT max(created) FROM surf_runs').fetchone()[0]
            if counts[0]>=ATTEMPTS_PER_DAY or (counts[1] or 0)>=SHARES_PER_DAY or (latest and now-latest<ATTEMPT_INTERVAL):
                return None
            identifier=uuid.uuid4().hex
            db.execute("INSERT INTO surf_runs(id,scope,day,created,state) VALUES(?,?,?,?,'claimed')",(identifier,scope,day,now))
            db.execute('UPDATE surf_channels SET reviewed=total WHERE scope=?',(scope,))
            db.execute('DELETE FROM surf_runs WHERE created<?',(now-90*86400,))
            return identifier

    def seen(self,url,title):
        url,title=canonical_url(url),title_key(title)
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM surf_runs WHERE created>? AND state IN ('sending','shared','unknown') AND (url=? OR (title<>'' AND title=?))",(time.time()-30*86400,url,title)).fetchone())

    def reserve_share(self,identifier,url,title):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute("SELECT * FROM surf_runs WHERE id=? AND state='claimed'",(identifier,)).fetchone()
            if not row:
                return False
            count=db.execute("SELECT count(*) FROM surf_runs WHERE day=? AND state IN ('sending','shared','unknown')",(row['day'],)).fetchone()[0]
            canonical,key=canonical_url(url),title_key(title)
            duplicate=db.execute("SELECT 1 FROM surf_runs WHERE created>? AND state IN ('sending','shared','unknown') AND (url=? OR (title<>'' AND title=?))",(time.time()-30*86400,canonical,key)).fetchone()
            if count>=SHARES_PER_DAY or duplicate:
                return False
            db.execute("UPDATE surf_runs SET state='sending',url=?,title=? WHERE id=?",(canonical,key,identifier))
            return True

    def finish(self,identifier,state):
        if state not in {'skipped','cancelled','failed','shared','unknown'}:
            raise ValueError('Invalid surfing outcome.')
        with self.db() as db:
            db.execute('UPDATE surf_runs SET state=? WHERE id=?',(state,identifier))

    def settle_unused(self,identifier):
        with self.db() as db:
            db.execute("UPDATE surf_runs SET state='unknown' WHERE id=? AND state='sending'",(identifier,))
            db.execute("UPDATE surf_runs SET state='skipped' WHERE id=? AND state='claimed'",(identifier,))

    def retire(self):
        with self.db() as db:
            db.execute("UPDATE surf_runs SET state='unknown' WHERE state='sending'")
            db.execute("UPDATE surf_runs SET state='cancelled' WHERE state='claimed'")
