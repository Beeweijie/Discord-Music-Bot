"""Bounded asynchronous model transport with durable request reservations."""
import asyncio
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

class ModelUnavailable(RuntimeError):
    pass

class DeepSeekClient:
    def __init__(self, data_dir, *, key=None, base_url=None):
        self.key = key if key is not None else os.getenv("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self.path = Path(data_dir) / "usage.sqlite3"
        self.session = None
        self.slots = asyncio.Semaphore(2)
        self.accounting = asyncio.Lock()
        self.failures = 0
        self.retry_at = 0

    @property
    def configured(self):
        return bool(self.key.strip())

    @staticmethod
    def _schema(db):
        db.execute("CREATE TABLE IF NOT EXISTS usage (id TEXT PRIMARY KEY, day TEXT, scope TEXT, stage TEXT, model TEXT, status TEXT, input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0, created REAL)")
        columns = {row[1] for row in db.execute('PRAGMA table_info(usage)')}
        for name in ('cache_hit_tokens', 'cache_miss_tokens', 'cache_reported'):
            if name not in columns:
                db.execute(f'ALTER TABLE usage ADD COLUMN {name} INTEGER DEFAULT 0')

    def _reserve(self, scope, stage, model, limit):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            self._schema(db)
            day = datetime.now(timezone.utc).date().isoformat()
            global_limit = int(os.getenv("DEEPSEEK_DAILY_REQUEST_LIMIT", "1000"))
            total = db.execute("SELECT count(*) FROM usage WHERE day=?", (day,)).fetchone()[0]
            family = stage.split('_')[0] + '_%'
            scoped = db.execute("SELECT count(*) FROM usage WHERE day=? AND scope=? AND stage LIKE ?", (day, scope, family)).fetchone()[0]
            tokens = db.execute("SELECT coalesce(sum(input_tokens+output_tokens),0) FROM usage WHERE day=?", (day,)).fetchone()[0]
            if total >= global_limit or scoped >= limit or tokens >= int(os.getenv("DEEPSEEK_DAILY_TOKEN_LIMIT", "1000000")):
                raise ModelUnavailable("Daily model budget reached.")
            request_id = uuid.uuid4().hex
            db.execute("INSERT INTO usage(id,day,scope,stage,model,status,created) VALUES(?,?,?,?,?,'reserved',?)", (request_id, day, scope, stage, model, time.time()))
            return request_id

    def _finish(self, request_id, status, usage):
        usage = usage if isinstance(usage, dict) else {}
        def tokens(key):
            value = usage.get(key, 0)
            return min(value, 2**31 - 1) if type(value) is int and value > 0 else 0
        total = tokens('prompt_tokens')
        hit, miss = usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens')
        reported = type(hit) is int and type(miss) is int and 0 <= hit <= total and hit + miss == total
        details = usage.get('prompt_tokens_details')
        if not reported and isinstance(details, dict):
            hit = details.get('cached_tokens')
            reported = type(hit) is int and 0 <= hit <= total
            miss = total - hit if reported else 0
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE usage SET status=?,input_tokens=?,output_tokens=?,cache_hit_tokens=?,cache_miss_tokens=?,cache_reported=? WHERE id=?",
                       (status, total, tokens('completion_tokens'), hit if reported else 0, miss if reported else 0, int(reported), request_id))

    async def complete(self, stage, messages, *, scope, model="deepseek-flash", json_mode=False, max_tokens=500, daily_limit=300):
        if not self.configured:
            raise ModelUnavailable("Set DEEPSEEK_API_KEY in the local .env file.")
        if time.monotonic() < self.retry_at:
            raise ModelUnavailable("Model service is cooling down after repeated failures.")
        async with self.slots:
            async with self.accounting:
                request_id = await asyncio.to_thread(self._reserve, scope, stage, model, daily_limit)
            status, usage = "unknown", {}
            try:
                if self.session is None or self.session.closed:
                    self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
                body = {"model": model, "messages": messages, "max_tokens": max_tokens,
                        "stream": False, "thinking": {"type": "disabled"}}
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                async with self.session.post(self.base_url + "/chat/completions", json=body,
                                             headers={"Authorization": "Bearer " + self.key}) as response:
                    if response.status != 200:
                        raise ModelUnavailable(f"Model request failed (HTTP {response.status}).")
                    raw = await response.content.read(1_048_577)
                    if len(raw) > 1_048_576:
                        raise ModelUnavailable("Model response exceeded the size limit.")
                    result = json.loads(raw)
                usage = result.get("usage", {})
                content = result["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ModelUnavailable("Model returned empty content.")
                output = json.loads(content) if json_mode else content.strip()
                if json_mode and not isinstance(output, dict):
                    raise ModelUnavailable("Model JSON must be an object.")
                status = "completed"
                self.failures = 0
                return output
            except asyncio.CancelledError:
                raise
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, IndexError, TypeError, ModelUnavailable) as error:
                status = "failed"
                self.failures += 1
                if self.failures >= 3:
                    self.retry_at = time.monotonic() + 30
                if isinstance(error, ModelUnavailable):
                    raise
                raise ModelUnavailable("Model transport or response validation failed.") from error
            finally:
                await asyncio.shield(asyncio.to_thread(self._finish, request_id, status, usage))

    async def embed(self, text, *, scope='embedding'):
        base = os.getenv("EMBEDDING_BASE_URL", "").rstrip("/")
        model = os.getenv("EMBEDDING_MODEL", "")
        if not base or not model:
            return None
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        headers = {}
        if os.getenv("EMBEDDING_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["EMBEDDING_API_KEY"]
        async with self.accounting:
            request_id = await asyncio.to_thread(self._reserve, scope, 'embedding_request', model, int(os.getenv('EMBEDDING_DAILY_REQUEST_LIMIT','300')))
        status, usage = 'unknown', {}
        try:
            async with self.slots, self.session.post(base + "/embeddings", json={"model": model, "input": text[:8000]}, headers=headers) as response:
                if response.status != 200:
                    raise ModelUnavailable(f"Embedding request failed (HTTP {response.status}).")
                raw = await response.content.read(1_048_577)
                if len(raw) > 1_048_576:
                    raise ModelUnavailable("Embedding response too large.")
                result = json.loads(raw)
                vector = result["data"][0]["embedding"]
                usage = result.get('usage',{})
            if not isinstance(vector, list) or not 1 <= len(vector) <= 8192 or any(type(x) not in {int, float} or not math.isfinite(x) for x in vector):
                raise ModelUnavailable("Invalid embedding vector.")
            status = 'completed'
            return model, vector
        except asyncio.CancelledError:
            raise
        except Exception as error:
            status = 'failed'
            raise ModelUnavailable('Embedding unavailable; keyword recall remains usable.') from error
        finally:
            await asyncio.shield(asyncio.to_thread(self._finish, request_id, status, usage))

    async def usage(self, scope_prefix):
        if not self.path.exists():
            return []
        def read():
            with closing(sqlite3.connect(self.path)) as db, db:
                self._schema(db)
                db.row_factory = sqlite3.Row
                return [dict(row) for row in db.execute("SELECT day,model,stage,status,count(*) requests,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(cache_hit_tokens) cache_hit_tokens,sum(cache_miss_tokens) cache_miss_tokens,sum(cache_reported) cache_reported FROM usage WHERE scope LIKE ? GROUP BY day,model,stage,status ORDER BY day DESC LIMIT 100", (scope_prefix + "%",))]
        return await asyncio.to_thread(read)

    async def close(self):
        if self.session is not None:
            await self.session.close()
