"""Public memory service: background work and access through scoped operations."""
import asyncio
import functools
import json
import logging
import time
import os
from concurrent.futures import ThreadPoolExecutor
from .prompts import EXTRACT, VERSION
from bot_app.shared.redaction import contains_secret

logger = logging.getLogger(__name__)

class MemoryService:
    def __init__(self, repository, model, settings_for, allowed_scope, author_allowed=None, timezone_for=None):
        self.repository = repository
        self.model = model
        self.settings_for = settings_for
        self.allowed_scope = allowed_scope
        self.author_allowed = author_allowed or (lambda scope, author: True)
        self.timezone_for = timezone_for or (lambda scope: 'UTC')
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-db")
        self.task = None
        self.closing = False
        self.error = None
        self.semantic_state = "disabled"
        self.scopes = set()
        self.on_forget = None

    async def call(self, method, *args, **kwargs):
        return await asyncio.get_running_loop().run_in_executor(self.executor, functools.partial(getattr(self.repository, method), *args, **kwargs))

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._work(), name="memory.background")

    def config(self, scope):
        return self.settings_for(scope.split(':')[1])

    @property
    def has_store(self):
        return self.repository.path.exists()

    async def recent_context(self, scope, limit):
        from bot_app.shared.identity import MessageEvent
        rows = await self.call('recent', scope.key, limit)
        return [MessageEvent(scope, row['id'], row['author'], row['name'], row['content'], row['created'],
                             is_bot=bool(row['bot'])) for row in rows]

    async def remember(self, event):
        """Explicit member note, with the same admission and source guards as ingestion."""
        if not await self.ingest(event):
            return False
        rows = await self.call('recent', event.scope.key, 100)
        source = next((row for row in rows if row['id'] == event.message_id), None)
        if source is None:
            return False
        created = await self.call('apply', {'id':'manual:' + event.message_id, 'scope':event.scope.key, 'payload':[source]},
            [{'kind':'fact', 'subject_id':event.author_id, 'content':event.content, 'source_ids':[event.message_id],
              'attribution':'self_report', 'slot':'explicit_note'}])
        return bool(created)

    async def ingest(self, event, observed=False):
        cfg = self.config(event.scope.key)
        if not cfg['enabled'] or not self.allowed_scope(event.scope.key) or not self.author_allowed(event.scope.key, event.author_id) or contains_secret(event.retained_text):
            return False
        changed = await self.call('ingest', event, observed)
        self.scopes.add(event.scope.key)
        if changed and not event.is_bot:
            force = any(word in event.content.casefold() for word in ('记住', '记错', '更正', 'remember this'))
            await self.call('queue', event.scope.key, batch_size=cfg['batch_messages'], wait_seconds=cfg['batch_seconds'], force=force)
        return changed

    async def recall(self, scope, query, subject=None, *, lightweight=False):
        cfg = self.config(scope)
        if not cfg['enabled'] or not self.allowed_scope(scope):
            return []
        embedding = None
        if cfg['semantic_search'] and not lightweight:
            try:
                embedding = await self.model.embed(query, scope=scope)
                self.semantic_state = 'ready' if embedding else 'missing_embedding_configuration'
            except Exception:
                self.semantic_state = 'unavailable_using_keywords'
        rows = await self.call('search', scope, query, subject=subject,
                               limit=min(3, cfg['recall_limit']) if lightweight else cfg['recall_limit'], embedding=embedding)
        recent = await self.call('list', scope, limit=20)
        summary = next((row for row in recent if row['kind'] == 'summary'), None)
        if summary and summary['id'] not in {row['id'] for row in rows}:
            rows = rows[:max(0, cfg['recall_limit'] - 1)] + [summary]
        rows = [row for row in rows if (not row['subject'] or self.author_allowed(scope, row['subject']))
                and all(self.author_allowed(scope, source['author']) for source in row['sources'])]
        return [{key: row[key] for key in ('id', 'subject', 'kind', 'content', 'created', 'valid_until', 'attribution')} for row in rows]

    async def epoch(self, scope):
        return await self.call('epoch', scope)

    async def delete_message(self, scope, message_id):
        if self.repository.path.exists():
            await self.call('delete_message', scope, message_id)

    async def list(self, scope, subject=None, candidates=False, limit=100, offset=0, history=False):
        return await self.call('list', scope, subject=subject, candidates=candidates, limit=limit, offset=offset, history=history)

    async def forget(self, scope, actor=None, memory_id=None):
        result = await self.call('forget', scope, actor=actor, memory_id=memory_id)
        if self.on_forget:
            await self.on_forget(scope,actor)
        return result

    async def correct(self, scope, memory_id, text, actor=None):
        return await self.call('correct', scope, memory_id, text, actor=actor)

    async def approve(self, scope, memory_id):
        return await self.call('approve', scope, memory_id)

    async def stats(self, prefix):
        result = await self.call('stats', prefix)
        return dict(result, state='error' if self.error else 'ready', error=self.error,
                    semantic_search=self.semantic_state, prompt_version=VERSION)

    async def _work(self):
        last_cleanup = 0
        last_index = 0
        while not self.closing:
            try:
                if not self.scopes and not self.repository.path.exists():
                    await asyncio.sleep(2)
                    continue
                for scope in await self.call('pending_scopes'):
                    cfg = self.config(scope)
                    if cfg['enabled'] and self.allowed_scope(scope):
                        await self.call('queue', scope, batch_size=cfg['batch_messages'], wait_seconds=cfg['batch_seconds'])
                if time.monotonic() - last_cleanup > 3600:
                    for scope in await self.call('all_scopes'):
                        await self.call('cleanup', scope, self.config(scope)['retention_days'])
                    last_cleanup = time.monotonic()
                if getattr(self.model, 'configured', True) is False:
                    self.error = 'MissingAPIKey'
                    await asyncio.sleep(2)
                    continue
                if time.monotonic() - last_index > 30:
                    last_index = time.monotonic()
                    for scope in await self.call('all_scopes'):
                        cfg = self.config(scope)
                        if not cfg['enabled'] or not cfg['semantic_search'] or not self.allowed_scope(scope):
                            continue
                        if not os.getenv('EMBEDDING_MODEL') or not os.getenv('EMBEDDING_BASE_URL'):
                            self.semantic_state = 'missing_embedding_configuration'
                            continue
                        try:
                            for row in await self.call('unindexed', scope, os.environ['EMBEDDING_MODEL']):
                                embedding = await self.model.embed(row['content'], scope=scope)
                                if embedding:
                                    await self.call('vector', row['id'], embedding)
                            self.semantic_state = 'ready'
                        except Exception:
                            self.semantic_state = 'unavailable_using_keywords'
                scopes = [scope for scope in await self.call('all_scopes')
                          if self.config(scope)['enabled'] and self.allowed_scope(scope)]
                job = await self.call('claim', scopes)
                if not job:
                    await asyncio.sleep(2)
                    continue
                cfg = self.config(job['scope'])
                excluded = [row['id'] for row in job['payload'] if not self.author_allowed(job['scope'], row['author'])]
                if excluded:
                    await self.call('obsolete', job, excluded)
                    continue
                if not cfg['enabled'] or not self.allowed_scope(job['scope']):
                    await self.call('fail', job, 'Memory disabled or channel excluded.')
                    continue
                try:
                    messages = [{k: row[k] for k in ('id', 'author', 'name', 'content', 'created')} for row in job['payload']]
                    existing = await self.call('list', job['scope'], limit=20)
                    existing = [{k: row[k] for k in ('id','subject','kind','slot','content','created')} for row in existing
                                if (not row['subject'] or self.author_allowed(job['scope'],row['subject']))
                                and all(self.author_allowed(job['scope'],source['author']) for source in row['sources'])]
                    result = await self.model.complete('memory_extract', [{"role": "system", "content": EXTRACT},
                        {"role": "user", "content": json.dumps({"timezone": self.timezone_for(job['scope']), "messages": messages, "existing_memories":existing}, ensure_ascii=False)}],
                        scope=job['scope'], model=cfg['model'], json_mode=True, max_tokens=1800, daily_limit=cfg['daily_model_calls'])
                    if not isinstance(result.get('items'), list) or any(not isinstance(x, dict) for x in result['items']):
                        raise ValueError('Invalid memory extraction schema.')
                    if not self.allowed_scope(job['scope']) or not self.config(job['scope'])['enabled']:
                        raise ValueError('Memory scope disabled during extraction.')
                    excluded = [row['id'] for row in job['payload'] if not self.author_allowed(job['scope'], row['author'])]
                    if excluded:
                        await self.call('obsolete', job, excluded)
                        continue
                    summary = result.get('summary', '')
                    if not isinstance(summary, str):
                        raise ValueError('Invalid summary.')
                    created = await self.call('apply', job, result['items'], summary[:1800])
                    self.error = None
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self.error = type(error).__name__
                    await self.call('fail', job, self.error)
                    logger.warning('Memory extraction failed: %s', self.error)
            except asyncio.CancelledError:
                break
            except Exception as error:
                self.error = type(error).__name__
                logger.exception('Memory background task failed')
                await asyncio.sleep(5)

    async def close(self):
        if self.closing:
            return
        self.closing = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
