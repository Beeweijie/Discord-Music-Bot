"""Infrequent by design: two browsing attempts, one possible share per day."""
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

ATTEMPTS_PER_DAY = 2
SHARES_PER_DAY = 1
ATTEMPT_INTERVAL = 6 * 3600
STARTUP_OBSERVATION = 3600
MIN_NEW_MESSAGES = 20
MIN_IDLE = 120
RECENT_ACTIVITY = 30 * 60


def available(state,cfg,now,monotonic,started):
    if (not cfg.get('surf_enabled') or not cfg['enabled'] or cfg['mode']!='participate'
            or not cfg['web_enabled'] or not cfg['interests'].strip() or state is None
            or not state.send or not state.can_send or state.processed<state.sequence
            or (state.task is not None and not state.task.done()) or monotonic-started<STARTUP_OBSERVATION):
        return False
    human=next((row for row in reversed(state.messages) if not row.is_bot and row.author_id not in cfg['excluded_users']),None)
    if human is None or human.mentioned_user_ids or human.reply_to:
        return False
    idle=now-human.created_at
    return MIN_IDLE<=idle<=RECENT_ACTIVITY and (not state.last_sent or monotonic-state.last_sent>=MIN_IDLE)


def canonical_url(url):
    parsed=urlsplit(url)
    query=[(key,value) for key,value in parse_qsl(parsed.query) if not key.casefold().startswith('utm_')
           and key.casefold() not in {'fbclid','gclid','spm'}]
    return urlunsplit((parsed.scheme,parsed.netloc.casefold(),parsed.path.rstrip('/'),urlencode(query),''))


def title_key(title):
    normalized=re.sub(r'[^\w\u3400-\u9fff]','',title.casefold())[:160]
    return hashlib.sha256(normalized.encode()).hexdigest() if normalized else ''
