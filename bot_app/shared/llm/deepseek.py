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
from zoneinfo import ZoneInfo
from .pricing import NANO, WEB, SOURCE, money, reserve_cost, actual_cost

import aiohttp

class ModelUnavailable(RuntimeError):
    pass


class BudgetExceeded(ModelUnavailable):
    pass


class ThinkingLimitReached(ModelUnavailable):
    pass


POOL_LIMITS = {'surf':100_000_000}  # A sub-budget inside the shared daily CNY budget.


async def bounded_json(response):
    """read(n) may return only the first network chunk, including half a UTF-8 character."""
    raw=bytearray()
    async for chunk in response.content.iter_chunked(65536):
        raw.extend(chunk)
        if len(raw)>1_048_576:
            raise ModelUnavailable('API response exceeded the size limit.')
    return json.loads(raw)

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
        columns = {row[1] for row in db.execute('PRAGMA table_info(usage)')}
        for name, definition in (('budget_day', 'TEXT'), ('reserved_nano', 'INTEGER DEFAULT 0'),
                                 ('cost_nano', 'INTEGER DEFAULT 0'), ('priced', 'INTEGER DEFAULT 0'),
                                 ('budget_pool', "TEXT DEFAULT ''"), ('reasoning_tokens','INTEGER DEFAULT 0')):
            if name not in columns:
                db.execute(f'ALTER TABLE usage ADD COLUMN {name} {definition}')
        # Preserve old accounting; charge known historical usage conservatively once.
        for row in db.execute('SELECT id,model,stage,input_tokens,output_tokens,cache_hit_tokens,created,status FROM usage WHERE priced=0').fetchall():
            identifier, model, stage, prompt, output, hit, created, status = row
            day = datetime.fromtimestamp(created or 0, ZoneInfo(os.getenv('DEEPSEEK_BUDGET_TIMEZONE', 'Australia/Sydney'))).date().isoformat()
            try:
                cost = WEB if stage.startswith('web_') and status == 'completed' else actual_cost(model, prompt, output, hit)
            except ValueError:
                cost = 0  # legacy unknown models are historical only
            db.execute('UPDATE usage SET priced=1,cost_nano=?,budget_day=? WHERE id=?', (cost, day, identifier))

    @staticmethod
    def budget_day():
        return datetime.now(ZoneInfo(os.getenv('DEEPSEEK_BUDGET_TIMEZONE', 'Australia/Sydney'))).date().isoformat()

    def _reserve(self, scope, stage, model, limit, estimated=0, budget_pool=''):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            self._schema(db)
            day = datetime.now(timezone.utc).date().isoformat()
            global_limit = int(os.getenv("DEEPSEEK_DAILY_REQUEST_LIMIT", "0"))
            total = db.execute("SELECT count(*) FROM usage WHERE day=?", (day,)).fetchone()[0]
            family = stage.split('_')[0] + '_%'
            scoped = db.execute("SELECT count(*) FROM usage WHERE day=? AND scope=? AND stage LIKE ?", (day, scope, family)).fetchone()[0]
            tokens = db.execute("SELECT coalesce(sum(input_tokens+output_tokens),0) FROM usage WHERE day=?", (day,)).fetchone()[0]
            token_limit = int(os.getenv('DEEPSEEK_DAILY_TOKEN_LIMIT','0'))
            if (global_limit and total >= global_limit) or (limit and scoped >= limit) or (token_limit and tokens >= token_limit):
                raise ModelUnavailable("Daily model budget reached.")
            budget_day = self.budget_day()
            spent = db.execute('SELECT coalesce(sum(cost_nano + reserved_nano),0) FROM usage WHERE budget_day=?', (budget_day,)).fetchone()[0]
            if spent + estimated > money(os.getenv('DEEPSEEK_DAILY_BUDGET_CNY', '1')):
                raise BudgetExceeded('Daily CNY budget has insufficient unreserved funds.')
            if budget_pool:
                if budget_pool not in POOL_LIMITS:
                    raise ValueError('Unknown budget pool.')
                pool_spent=db.execute('SELECT coalesce(sum(cost_nano+reserved_nano),0) FROM usage WHERE budget_day=? AND budget_pool=?',(budget_day,budget_pool)).fetchone()[0]
                if pool_spent+estimated>POOL_LIMITS[budget_pool]:
                    raise BudgetExceeded('Daily browsing sub-budget has insufficient unreserved funds.')
            request_id = uuid.uuid4().hex
            db.execute("INSERT INTO usage(id,day,scope,stage,model,status,created,budget_day,reserved_nano,priced,budget_pool) VALUES(?,?,?,?,?,'reserved',?,?,?,1,?)", (request_id, day, scope, stage, model, time.time(), budget_day, estimated,budget_pool))
            return request_id

    def _finish(self, request_id, status, usage, *, billable=None, no_charge=False):
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
            model, stage, reserved = db.execute('SELECT model,stage,reserved_nano FROM usage WHERE id=?', (request_id,)).fetchone()
            if no_charge:
                cost = 0
            elif billable is not None:
                cost = WEB if billable else 0
            elif (type(usage.get('prompt_tokens')) is int and type(usage.get('completion_tokens')) is int
                  and usage['prompt_tokens']>=0 and usage['completion_tokens']>=0):
                cost = actual_cost(model, total, tokens('completion_tokens'), hit if reported else 0)
            else:
                cost = reserved  # timeout, crash or absent usage: never refund an uncertain bill
            completion_details=usage.get('completion_tokens_details') or {}
            reasoning=completion_details.get('reasoning_tokens',0) if isinstance(completion_details,dict) else 0
            reasoning=reasoning if type(reasoning) is int and 0<=reasoning<=tokens('completion_tokens') else 0
            db.execute("UPDATE usage SET status=?,input_tokens=?,output_tokens=?,cache_hit_tokens=?,cache_miss_tokens=?,cache_reported=?,cost_nano=?,reserved_nano=0,reasoning_tokens=? WHERE id=?",
                       (status, total, tokens('completion_tokens'), hit if reported else 0, miss if reported else 0, int(reported), cost, reasoning,request_id))

    async def complete(self, stage, messages, *, scope, model="deepseek-flash", json_mode=False, max_tokens=500, daily_limit=300,
                       tools=None, tool_choice='auto', thinking='none', budget_pool=''):
        if not self.configured:
            raise ModelUnavailable("Set DEEPSEEK_API_KEY in the local .env file.")
        if time.monotonic() < self.retry_at:
            raise ModelUnavailable("Model service is cooling down after repeated failures.")
        if thinking not in {'none','low','high'}:
            raise ValueError('Unsupported thinking effort.')
        if thinking!='none' and tools is not None:
            # Historic chat messages never retain private model reasoning. Current tool-loop
            # assistant messages preserve it in full until that one reply completes.
            messages=[dict(message,reasoning_content=message.get('reasoning_content') or '')
                      if message.get('role')=='assistant' else message for message in messages]
        async with self.slots:
            try:
                estimated = reserve_cost(model, messages, max_tokens, tools)
            except ValueError as error:
                raise ModelUnavailable(str(error)) from error
            async with self.accounting:
                request_id = await asyncio.to_thread(self._reserve, scope, stage, model, daily_limit, estimated,budget_pool)
            status, usage = "unknown", {}
            no_charge = False
            try:
                if self.session is None or self.session.closed:
                    self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
                body = {"model": model, "messages": messages, "max_tokens": max_tokens,
                        "stream": False, "thinking": {"type": "disabled" if thinking=='none' else 'enabled'}}
                if thinking!='none':
                    body['reasoning_effort']=thinking
                if json_mode:
                    body["response_format"] = {"type": "json_object"}
                if tools is not None:
                    body.update(tools=tools,tool_choice=tool_choice)
                async with self.session.post(self.base_url + "/chat/completions", json=body,
                                             headers={"Authorization": "Bearer " + self.key},
                                             timeout=aiohttp.ClientTimeout(total=45 if thinking!='none' else 30)) as response:
                    if response.status != 200:
                        no_charge = 400 <= response.status < 500
                        raise ModelUnavailable(f"Model request failed (HTTP {response.status}).")
                    result = await bounded_json(response)
                usage = result.get("usage", {})
                if thinking!='none' and result['choices'][0].get('finish_reason')=='length':
                    raise ThinkingLimitReached('The bounded thinking output was exhausted.')
                message = result['choices'][0]['message']
                content = message.get('content')
                if tools is not None:
                    calls = message.get('tool_calls') or []
                    if not isinstance(calls,list) or len(calls)>5:
                        raise ModelUnavailable('Invalid model tool calls.')
                    cleaned=[]
                    for call in calls:
                        if not isinstance(call,dict) or not isinstance(call.get('function'),dict):
                            raise ModelUnavailable('Invalid model tool call schema.')
                        function=call.get('function',{})
                        if (call.get('type')!='function' or not isinstance(call.get('id'),str)
                                or not 1<=len(call['id'])<=200 or not isinstance(function.get('name'),str)
                                or not isinstance(function.get('arguments'),str) or len(function['arguments'])>8192):
                            raise ModelUnavailable('Invalid model tool call schema.')
                        cleaned.append({'id':call['id'],'type':'function','function':{'name':function['name'],'arguments':function['arguments']}})
                    if not cleaned and (not isinstance(content,str) or not content.strip()):
                        raise ModelUnavailable('Model returned no answer or tool call.')
                    if content is not None and not isinstance(content,str):
                        raise ModelUnavailable('Invalid model message content.')
                    status='completed'
                    self.failures=0
                    assistant={'role':'assistant','content':content,'tool_calls':cleaned}
                    if thinking!='none':
                        reasoning=message.get('reasoning_content') or ''
                        if not isinstance(reasoning,str):
                            raise ModelUnavailable('Invalid model reasoning content.')
                        assistant['reasoning_content']=reasoning
                    return assistant
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
                await asyncio.shield(asyncio.to_thread(self._finish, request_id, status, usage, no_charge=no_charge))

    async def web_request(self, kind, arguments, *, scope, daily_limit=60, budget_pool=''):
        """DeepSeek's official read-only Search/Fetch APIs; no direct target-site requests."""
        if kind not in {'search','fetch'} or not self.configured:
            raise ModelUnavailable('Web search is not configured.')
        async with self.slots:
            async with self.accounting:
                request_id=await asyncio.to_thread(self._reserve,scope,'web_'+kind,'deepseek-'+kind,daily_limit,WEB,budget_pool)
            status='unknown'
            billable=None
            try:
                if self.session is None or self.session.closed:
                    self.session=aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
                base=self.base_url.removesuffix('/v1')
                async with self.session.post(base+'/v1/tools/'+kind,json=arguments,
                        headers={'Authorization':'Bearer '+self.key},timeout=aiohttp.ClientTimeout(total=15)) as response:
                    if response.status!=200:
                        billable=False
                        raise ModelUnavailable(f'Web request failed (HTTP {response.status}).')
                    result=await bounded_json(response)
                if not isinstance(result,dict):
                    raise ModelUnavailable('Invalid web response.')
                status='completed'
                billable=bool(result.get('results')) if kind=='search' else bool(result.get('content'))
                return result
            except asyncio.CancelledError:
                raise
            except Exception as error:
                status='failed'
                raise ModelUnavailable('Web retrieval unavailable; no evidence was obtained.') from error
            finally:
                await asyncio.shield(asyncio.to_thread(self._finish,request_id,status,{},billable=billable))

    async def embed(self, text, *, scope='embedding'):
        base = os.getenv("EMBEDDING_BASE_URL", "").rstrip("/")
        model = os.getenv("EMBEDDING_MODEL", "")
        if not base or not model:
            return None
        # External embedding prices are provider-specific. Require an explicit conservative
        # CNY ceiling per request before including one in the shared monetary budget.
        if not os.getenv('EMBEDDING_MAX_REQUEST_CNY'):
            raise ModelUnavailable('Set EMBEDDING_MAX_REQUEST_CNY to include this provider in the CNY budget.')
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        headers = {}
        if os.getenv("EMBEDDING_API_KEY"):
            headers["Authorization"] = "Bearer " + os.environ["EMBEDDING_API_KEY"]
        async with self.accounting:
            request_id = await asyncio.to_thread(self._reserve, scope, 'embedding_request', model, int(os.getenv('EMBEDDING_DAILY_REQUEST_LIMIT','300')),money(os.environ['EMBEDDING_MAX_REQUEST_CNY']))
        status, usage = 'unknown', {}
        try:
            async with self.slots, self.session.post(base + "/embeddings", json={"model": model, "input": text[:8000]}, headers=headers) as response:
                if response.status != 200:
                    raise ModelUnavailable(f"Embedding request failed (HTTP {response.status}).")
                result = await bounded_json(response)
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
            await asyncio.shield(asyncio.to_thread(self._finish, request_id, status, {}))

    async def usage(self, scope_prefix):
        if not self.path.exists():
            return []
        def read():
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute('BEGIN IMMEDIATE')
                self._schema(db)
                db.row_factory = sqlite3.Row
                return [dict(row) for row in db.execute("SELECT day,model,stage,status,count(*) requests,sum(input_tokens) input_tokens,sum(output_tokens) output_tokens,sum(cache_hit_tokens) cache_hit_tokens,sum(cache_miss_tokens) cache_miss_tokens,sum(cache_reported) cache_reported,sum(reasoning_tokens) reasoning_tokens,sum(cost_nano)/1000000000.0 cost_cny FROM usage WHERE scope LIKE ? GROUP BY day,model,stage,status ORDER BY day DESC LIMIT 100", (scope_prefix + "%",))]
        return await asyncio.to_thread(read)

    async def budget(self):
        def read():
            spent = reserved = 0
            pools={name:{'limit_cny':limit/NANO,'spent_cny':0,'reserved_cny':0,'remaining_cny':limit/NANO} for name,limit in POOL_LIMITS.items()}
            if self.path.exists():
                with closing(sqlite3.connect(self.path)) as db, db:
                    db.execute('BEGIN IMMEDIATE')
                    self._schema(db)
                    spent, reserved = db.execute('SELECT coalesce(sum(cost_nano),0),coalesce(sum(reserved_nano),0) FROM usage WHERE budget_day=?', (self.budget_day(),)).fetchone()
                    for name,limit in POOL_LIMITS.items():
                        used,held=db.execute('SELECT coalesce(sum(cost_nano),0),coalesce(sum(reserved_nano),0) FROM usage WHERE budget_day=? AND budget_pool=?',(self.budget_day(),name)).fetchone()
                        pools[name].update(spent_cny=used/NANO,reserved_cny=held/NANO,remaining_cny=max(0,limit-used-held)/NANO)
            limit = money(os.getenv('DEEPSEEK_DAILY_BUDGET_CNY', '1'))
            return dict(day=self.budget_day(), timezone=os.getenv('DEEPSEEK_BUDGET_TIMEZONE','Australia/Sydney'),
                        limit_cny=limit/NANO, spent_cny=spent/NANO, reserved_cny=reserved/NANO,
                        remaining_cny=max(0,limit-spent-reserved)/NANO, basis='conservative_peak', price_source=SOURCE,pools=pools)
        return await asyncio.to_thread(read)

    async def close(self):
        if self.session is not None:
            await self.session.close()
