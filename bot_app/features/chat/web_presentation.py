"""How to present retrieved evidence; lookup permissions live in tool rules."""
import re
from bot_app.features.web_search.api import public_url
from bot_app.shared.redaction import contains_secret


def web_presentation(text):
    requested=bool(re.search(r'出处|来源|原文|参考文献|官方文档|证据|网址|链接|哪(?:里|儿).{0,4}(?:看|查|找)|'
                             r'\b(?:sources?|citations?|links?|references?)\b',text,re.I))
    verification=bool(re.search(r'核实|查证|辟谣|真假|最新|新闻|政策|法规|法律|价格|多少钱|汇率|利率|'
                                r'用药|药物|症状|诊断|剂量|版本.{0,8}(?:更新|发布)|更新.{0,8}(?:版本|内容)',text))
    return {'mode':'sources' if requested or verification else 'chat',
            'explicit_sources':requested,'max_links':2 if requested and re.search(r'几个|多个|文献|references',text,re.I) else 1}


def result_sources(result):
    """Only verified public URLs returned by a successful tool, never model links."""
    rows=result.get('results',[]) or ([result] if result.get('url') else [])
    sources=[]
    for row in rows[:5]:
        try:
            url=public_url(row.get('url'))
        except (ValueError,TypeError,AttributeError):
            continue
        title=str(row.get('title') or '')[:180]
        if contains_secret(title) or any(item['url']==url for item in sources):
            continue
        sources.append({'title':title,'url':url})
    return sources
