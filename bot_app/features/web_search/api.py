"""Public search interface backed by DeepSeek's official Search/Fetch API."""
import ipaddress
import re
import time
from collections import OrderedDict
from urllib.parse import urlsplit,parse_qsl
from bot_app.shared.redaction import contains_secret


def public_url(value):
    if not isinstance(value,str) or not 1<=len(value)<=2048 or contains_secret(value):
        raise ValueError('Supply a public HTTP(S) URL.')
    try:
        url=urlsplit(value)
        host=(url.hostname or '').lower().rstrip('.')
        port=url.port
    except ValueError:
        raise ValueError('Invalid public URL.') from None
    if (url.scheme not in {'http','https'} or not host or url.username or url.password
            or port not in {None,80,443} or host=='localhost' or '.' not in host
            or host.endswith(('.local','.localhost','.internal','.lan'))
            or any(key.casefold() in {'token','api_key','access_token','password','auth','signature'} for key,_ in parse_qsl(url.query))):
        raise ValueError('Only public URLs without credentials are supported.')
    try:
        address=ipaddress.ip_address(host)
    except ValueError:
        address=None
    if address is not None and not address.is_global:
        raise ValueError('Private or reserved network addresses cannot be fetched.')
    return value


class WebSearchService:
    def __init__(self,model):
        self.model=model
        self.state='not_used'
        self.cache=OrderedDict()

    async def execute(self,name,arguments,*,scope,daily_limit,budget_pool=''):
        if not isinstance(arguments,dict):
            raise ValueError('Tool arguments must be an object.')
        if name=='web_search':
            query=arguments.get('query')
            if (not isinstance(query,str) or not 1<=len(query.strip())<=128 or contains_secret(query)
                    or re.search(r'<@|\b\d{17,20}\b',query)):
                raise ValueError('Use a short public search query without member IDs or credentials.')
            kind,payload='search',{'query':query.strip(),'count':5,'need_content':True}
        elif name=='read_webpage':
            kind,payload='fetch',{'url':public_url(arguments.get('url'))}
        else:
            raise ValueError('Unsupported web tool.')
        key=(scope,kind,str(payload))
        cached=self.cache.get(key)
        if cached and time.monotonic()-cached[0]<120:
            self.cache.move_to_end(key)
            return cached[1]
        try:
            options={'scope':scope,'daily_limit':daily_limit}
            if budget_pool:
                options['budget_pool']=budget_pool
            raw=await self.model.web_request(kind,payload,**options)
            if kind=='search':
                if not isinstance(raw.get('results'),list):
                    raise ValueError('Invalid search response.')
                results=[]
                for item in raw['results'][:5]:
                    if not isinstance(item,dict):
                        continue
                    try:
                        url=public_url(item.get('url'))
                    except ValueError:
                        continue
                    results.append({'title':str(item.get('title') or '')[:250],'url':url,
                        'date':str(item.get('date') or '')[:60],
                        'summary':str(item.get('summary') or item.get('snippet') or '')[:1200],
                        'content':str(item.get('content') or '')[:1800]})
                result={'results':results}
            else:
                if not isinstance(raw.get('content'),str):
                    raise ValueError('Invalid fetched page.')
                result={'url':payload['url'],'title':str(raw.get('title') or '')[:250], 'content':raw['content'][:9000]}
            self.state='ready'
            self.cache[key]=(time.monotonic(),result)
            while len(self.cache)>128:
                self.cache.popitem(last=False)
            return result
        except Exception:
            self.state='unavailable'
            raise
