"""Bounded model-directed, read-only web tool loop."""
import json
import re
from .prompts import REPLY
from .thinking import ThinkingPlan
from .web_presentation import web_presentation, result_sources
from bot_app.shared.llm.deepseek import ThinkingLimitReached

TOOLS=[{'type':'function','function':{'name':'web_search','description':'Search public web sources when current facts, news, uncertainty or verification require it. Avoid searching for casual chat. Use generic queries without private chat or member identifiers.',
    'parameters':{'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False}}},
    {'type':'function','function':{'name':'read_webpage','description':'Read a public URL supplied in the current message or returned by web_search.',
    'parameters':{'type':'object','properties':{'url':{'type':'string'}},'required':['url'],'additionalProperties':False}}}]


class ReplyGenerator:
    def __init__(self,model,web=None,on_web=None):
        self.model,self.web=model,web
        self.on_web=on_web

    async def generate(self,prefix,task,payload,cfg,*,scope,current,still_current,record_tool,thinking=None):
        payload=dict(payload,web_presentation=web_presentation(current.content))
        messages=prefix+[task(REPLY,payload)]
        thinking=thinking or ThinkingPlan(max_tokens=cfg.get('reply_max_tokens',300))
        options={'scope':scope,'model':cfg['model'],'max_tokens':thinking.max_tokens,
                 'thinking':thinking.effort,'daily_limit':cfg['daily_model_calls']}
        fell_back=False
        async def complete(**extra):
            nonlocal fell_back
            try:
                return await self.model.complete('chat_reply',messages,**options,**extra)
            except ThinkingLimitReached:
                if fell_back or options['thinking']=='none':
                    raise
                fell_back=True
                options.update(thinking='none',max_tokens=cfg.get('reply_max_tokens',300))
                await record_tool('thinking','bounded_fast_fallback')
                if not await still_current():
                    return None
                return await self.model.complete('chat_reply',messages,**options,**extra)
        if not cfg.get('web_enabled') or self.web is None:
            return await complete()
        allowed_urls=set(url.rstrip(').,;') for url in re.findall(r'https?://[^\s<>]+',current.content))
        used=0
        for _ in range(cfg['web_max_calls']+2):
            if not await still_current():
                return None
            message=await complete(tools=TOOLS,tool_choice='auto' if used<cfg['web_max_calls'] else 'none')
            if message is None:
                return None
            calls=message.get('tool_calls') or []
            if not calls:
                return message.get('content') or ''
            messages.append(message)
            for call in calls:
                if not await still_current():
                    return None
                name=call['function']['name']
                try:
                    if used>=cfg['web_max_calls']:
                        raise ValueError('This reply has reached its web tool limit. Answer now using available evidence.')
                    used+=1
                    if name not in {'web_search','read_webpage'}:
                        raise ValueError('Only public web search and page reading are supported.')
                    arguments=json.loads(call['function']['arguments'])
                    if name=='read_webpage' and (not isinstance(arguments,dict) or arguments.get('url') not in allowed_urls):
                        raise ValueError('Read only URLs supplied in the current message or returned by web_search.')
                    result=await self.web.execute(name,arguments,scope=scope,daily_limit=cfg['web_daily_calls'])
                    allowed_urls.update(row['url'] for row in result.get('results',[]))
                    await record_tool(name,'completed',{'sources':result_sources(result)})
                    if self.on_web:
                        await self.on_web(scope,result)
                except Exception:
                    result={'error':'Web retrieval failed or was not allowed. No new evidence was obtained; do not claim successful lookup.'}
                    await record_tool(name,'failed')
                messages.append({'role':'tool','tool_call_id':call['id'],'content':json.dumps(result,ensure_ascii=False)})
        return None
