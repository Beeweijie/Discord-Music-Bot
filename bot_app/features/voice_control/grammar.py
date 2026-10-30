"""Small, explicit voice grammar. Ordinary conversation never opens the gate."""
import re
import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class VoiceCommand:
    action: str
    query: str = ""


@dataclass(frozen=True)
class SpeechResult:
    text: str
    heard_wake: bool = False
    alternate_text: str = ""
    diagnostics: dict = field(default_factory=dict)


def clean_speech(text):
    text = re.sub(r"<\|.*?\|>", "", text)
    text = "".join(c for c in unicodedata.normalize("NFKC", text)
                   if not unicodedata.category(c).startswith("P"))
    # Preserve spaces in English artist/song names, remove ASR spaces in Chinese.
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", text).strip()


def intent_text(text):
    return re.sub(r'^(?:呃|嗯|啊|诶|欸|哎)+', '', clean_speech(text)).strip()


# SenseVoice sometimes transcribes the name with homophones. Still require two
# complete names at the beginning; a single name or a mention mid-sentence fails.
NAME_END = r"[酱醬将將江疆姜讲講降]"
NAME = r"[喵苗妙瞄猫貓秒淼]" + NAME_END
WAKE = re.compile(r"^(?:" + NAME + r"){2,}")
# Real microphone tests lose the first 喵 in both ASR and acoustic KWS.
# Require the remaining three syllables plus a valid command, never just one
# complete name. Examples: 将喵将下一首歌 / 将喵降下一首歌吧.
CLIPPED_WAKE = re.compile(r"^" + NAME_END + r"(?:" + NAME + r")+")
SINGLE_NAME = re.compile(r"^" + NAME)
FILLER = re.compile(r"^(?:(?:请|請|帮我|幫我|给我|給我|麻烦|麻煩|嗯|啊|哎|欸|那个|那個))+")
COMMAND_START = re.compile(r"下一|切歌|换一首|換一首|跳过|跳過|暂停|暫停|继续|繼續|播放|放一首|放首|来一首|來一首|点歌|點歌|加入|添加到列表|加到列表")
QUIET_WORDS = {'闭嘴', '閉嘴', '安静', '安靜', '不要说话', '不要說話', '别说了', '別說了',
               '停止监听', '停止監聽', '取消', '算了', '取消选择', '取消選擇', '退出选择', '退出選擇', '退出'}


def is_wake_text(text):
    text = intent_text(text)
    return bool(WAKE.match(text) or CLIPPED_WAKE.match(text))


def is_quiet_text(text):
    text = intent_text(text)
    wake = WAKE.match(text) or CLIPPED_WAKE.match(text)
    if wake:
        text = text[wake.end():]
    text = FILLER.sub('', text).removesuffix('吧')
    return text in QUIET_WORDS


def embedded_command(text):
    text = intent_text(text)
    for match in COMMAND_START.finditer(text):
        tail = text[match.start():]
        if parse_command(tail):
            return tail
    return None


def parse_command(text):
    text = FILLER.sub("", text.strip())
    controls = {
        "下一首": "skip", "下一首歌": "skip", "切歌": "skip", "换一首": "skip",
        "換一首": "skip", "跳过": "skip", "跳過": "skip",
        "下一首歌曲": "skip", "下一手": "skip", "下一手歌": "skip",
        "暂停": "pause", "暫停": "pause", "暂停播放": "pause", "暫停播放": "pause",
        "继续": "resume", "繼續": "resume", "继续播放": "resume", "繼續播放": "resume",
        "播放歌单": "playqueue", "播放歌單": "playqueue", "播放列表": "playqueue",
        "播放队列": "playqueue", "播放隊列": "playqueue", "继续歌单": "playqueue", "繼續歌單": "playqueue",
    }
    control = re.sub(r"(?:吧|呀|啊|啦|谢谢|謝謝|谢谢你|謝謝你)$", "", text.replace(" ", ""))
    if control in controls:
        return VoiceCommand(controls[control])
    match = re.fullmatch(r"(?:加入列表|加入队列|加入隊列|添加到列表|添加到队列|加到列表|加入歌单|加入歌單|加入)\s*(.{1,100})", text)
    if match and match[1].strip():
        return VoiceCommand("enqueue", match[1].strip())
    match = re.fullmatch(r"(?:播放|放一首|放首|来一首|來一首|点歌|點歌)\s*(.{1,100})", text)
    if match:
        query = match[1].strip()
        if query:
            return VoiceCommand("play", query)
    return None


class WakeGate:
    def __init__(self):
        self.armed = {}
        self.partial = {}
        self.last_reason = "ordinary"

    def arm(self, user_id, now):
        self.armed[user_id] = now + 8

    def accept(self, user_id, text, now, heard_wake=False):
        self.armed = {key: expiry for key, expiry in self.armed.items() if expiry >= now}
        self.partial = {key: expiry for key, expiry in self.partial.items() if expiry >= now}
        self.last_reason = "ordinary"
        text = intent_text(text)
        if not text and not heard_wake:
            self.last_reason = "empty"
            return None
        match = WAKE.match(text)
        clipped = CLIPPED_WAKE.match(text) if not match else None
        if clipped:
            tail = text[clipped.end():]
            if not tail:
                self.arm(user_id, now)
                self.last_reason = "clipped_wake"
                return VoiceCommand("wake")
            command = parse_command(tail)
            if command:
                self.armed.pop(user_id, None)
                self.partial.pop(user_id, None)
                self.last_reason = "clipped_wake"
                return command
        if not match and user_id in self.partial:
            match = SINGLE_NAME.match(text)
            if match:
                self.partial.pop(user_id, None)
                self.last_reason = "split_wake"
        if match:
            self.last_reason = self.last_reason if self.last_reason == "split_wake" else "text_wake"
            self.armed.pop(user_id, None)
            tail = text[match.end():]
            if not tail:
                self.arm(user_id, now)
                return VoiceCommand("wake")
            command = parse_command(tail)
            if command:
                return command
            self.last_reason = "unsupported_after_wake"
            self.arm(user_id, now)
            return VoiceCommand("unclear")
        if heard_wake:
            self.last_reason = "audio_wake"
            self.partial.pop(user_id, None)
            self.armed.pop(user_id, None)
            # The sound was a double wake even if ASR spelled it differently.
            # Only a complete music command after that wake can be dispatched.
            for match in COMMAND_START.finditer(text):
                command = parse_command(text[match.start():])
                if command:
                    return command
            self.arm(user_id, now)
            return VoiceCommand("wake")
        match = SINGLE_NAME.fullmatch(text)
        if match:
            self.partial[user_id] = now + 3
            self.last_reason = "partial_wake"
            return None
        expiry = self.armed.pop(user_id, 0)
        if expiry >= now:
            command = parse_command(text)
            self.last_reason = "armed_command" if command else "unsupported_armed"
            if not command:
                self.armed[user_id] = expiry
            return command
        return None
