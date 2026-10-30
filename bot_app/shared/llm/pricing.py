"""Conservative CNY accounting. Official peak rates verified 2026-10-31.

Use integer nano-yuan, never floats, for reservations. Peak rates deliberately
overestimate off-peak/holiday bills. Cache discounts use reported token counts.
"""
import json
from decimal import Decimal, InvalidOperation

NANO = 1_000_000_000
SOURCE = 'https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
FLASH = (40, 2000, 8000)  # cached input, uncached input, output; nano-CNY/token
PRO = (300, 9000, 27000)
WEB = 5_000_000  # 5 CNY / 1000 successful nonempty search/fetch requests


def money(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > 10000:
            raise ValueError('Invalid daily CNY budget.')
        return int(amount * NANO)
    except InvalidOperation:
        raise ValueError('Invalid daily CNY budget.') from None


def rates(model):
    if model in {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp',
                 'deepseek-chat', 'deepseek-reasoner'}:
        return FLASH
    if model == 'deepseek-v4-pro':
        return PRO
    raise ValueError('No verified CNY price for this model; configure a supported DeepSeek model.')


def reserve_cost(model, messages, max_tokens, tools=None):
    """UTF-8 bytes bound text tokens; each image has an official 1024-token cap."""
    encoded = 512
    images = 0
    for message in messages:
        content = message.get('content')
        if isinstance(content, list):
            cleaned = []
            for part in content:
                if part.get('type') == 'image_url':
                    images += 1
                else:
                    cleaned.append(part)
            message = dict(message, content=cleaned)
        encoded += len(json.dumps(message, ensure_ascii=False).encode('utf-8')) + 64
    if tools:
        encoded += len(json.dumps(tools, ensure_ascii=False).encode('utf-8')) + 256
    _, miss, output = rates(model)
    return (encoded + images * 1024) * miss + max_tokens * output


def actual_cost(model, total, output, hit=0):
    cached, uncached, generated = rates(model)
    return hit * cached + (total - hit) * uncached + output * generated
