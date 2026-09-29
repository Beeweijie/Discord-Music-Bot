"""Readable media failures shared by UI transports."""
import html
import re


def clean_media_error(error):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", html.unescape(str(error)))
    return re.sub(r"\s+", " ", text).strip()[:400]
