"""Optional semantic message breaks, generated once and delivered in a small burst."""

BREAK='<message_break>'


def reply_parts(text,cfg):
    parts=[part.strip() for part in text.split(BREAK) if part.strip()]
    if not cfg.get('reply_burst_enabled') or '```' in text:
        return ['\n'.join(parts)] if parts else []
    # No punctuation-based forcing: an ordinary response without explicit breaks stays one message.
    return parts[:2]+['\n'.join(parts[2:])] if len(parts)>3 else parts
