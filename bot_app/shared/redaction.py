"""Conservative exclusion of explicitly labelled credentials from AI inputs."""
import re
_SECRET = re.compile(r"(?:DISCORD_TOKEN|DEEPSEEK_API_KEY|api[_ -]?key|password)\s*[:=]\s*\S+|\bmfa\.[\w-]+|\bsk-[A-Za-z0-9_-]{20,}", re.I)

def contains_secret(text):
    return bool(_SECRET.search(text))
