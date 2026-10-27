"""Music-name association using pronunciation and the listener's song context."""
import re
from dataclasses import dataclass

SONGS = {
    "雨爱": "杨丞琳", "晴天": "周杰伦", "七里香": "周杰伦", "稻香": "周杰伦",
    "夜曲": "周杰伦", "花海": "周杰伦", "告白气球": "周杰伦", "搁浅": "周杰伦",
    "一路向北": "周杰伦", "青花瓷": "周杰伦", "等你下课": "周杰伦", "简单爱": "周杰伦",
    "爱你": "王心凌", "遇见": "孙燕姿", "后来": "刘若英", "小幸运": "田馥甄",
    "光年之外": "邓紫棋", "泡沫": "邓紫棋", "孤勇者": "陈奕迅", "十年": "陈奕迅",
    "富士山下": "陈奕迅", "爱情转移": "陈奕迅", "演员": "薛之谦", "年少有为": "李荣浩",
    "起风了": "", "有何不可": "许嵩", "庐州月": "许嵩", "如果当时": "许嵩",
    "断桥残雪": "许嵩", "玫瑰花的葬礼": "许嵩", "夜空中最亮的星": "逃跑计划",
    "追光者": "岑宁儿", "体面": "于文文", "乌梅子酱": "李荣浩", "水星记": "郭顶",
    "心雨": "周杰伦", "暗号": "周杰伦", "安静": "周杰伦", "说好的幸福呢": "周杰伦",
}
ARTISTS = tuple(dict.fromkeys([v for v in SONGS.values() if v] + [
    "林俊杰", "王菲", "张杰", "张学友", "周深", "林宥嘉", "五月天", "梁静茹", "蔡依林",
    "伍佰", "张惠妹", "李健", "毛不易", "张韶涵", "许巍", "陈粒", "汪苏泷"]))
TRADITIONAL = str.maketrans("愛雲後來風說時歡聽飛龍與學傑倫陳楊淺號靜願轉離", "爱云后来风说时欢听飞龙与学杰伦陈杨浅号静愿转离")
MUSIC_WORDS = ("official", "music", "lyrics", "lyric", "audio", " mv", "mv ", "歌詞", "歌词",
               "演唱", "歌曲", "翻唱", "原唱", "cover", "live", "topic", "ost", "原声", "原聲")
NON_MUSIC_WORDS = ("曾仕强", "曾仕強", "人生智慧", "修行", "元神是否", "归位", "歸位",
                   "講座", "讲座", "课程", "課程", "赚钱", "賺錢", "玄学", "玄學", "带货", "帶貨",
                   "游戏攻略", "遊戲攻略", "實況", "实况", "直播回放")


def normalized(text):
    return re.sub(r"[^a-z0-9\u3400-\u9fff]", "", text.lower().translate(TRADITIONAL))


@dataclass(frozen=True)
class QueryGuess:
    original: str
    query: str
    alternatives: tuple = ()
    artist: str = ""


class MusicAssociator:
    def __init__(self, phonetic):
        self.phonetic = phonetic

    def guess(self, query, titles=()):
        original = query.strip()
        if not re.search(r"[\u3400-\u9fff]", original):
            return QueryGuess(original, original)
        text = normalized(original)
        if len(text) > 2 and text.endswith(("吧", "呀", "啊")) and text not in SONGS:
            text = text[:-1]
        # Explicit correction supplied by the owner outranks a mistaken title
        # that may already have entered the queue through the old ASR path.
        if text == '与爱':
            return QueryGuess(original, '雨爱', artist='杨丞琳')
        words = list(SONGS) + list(ARTISTS)
        sound = self.phonetic(text)
        # Extract matching name spans from the actual server queue/history/library,
        # including artist - title and Traditional Chinese metadata.
        for title in titles[:1200]:
            if any(word in title for word in NON_MUSIC_WORDS):
                continue
            for run in re.findall(r"[\u3400-\u9fff]{2,80}", title):
                phones = self.phonetic(run)
                width = len(sound)
                if not 2 <= width <= 16:
                    continue
                for start in range(len(phones) - width + 1):
                    if phones[start:start + width] == sound:
                        words.append(run[start:start + width].translate(TRADITIONAL))
        matches = list(dict.fromkeys(w for w in words if self.phonetic(normalized(w)) == sound))
        if text in matches:
            return QueryGuess(original, text, artist=SONGS.get(text, ""))
        if len(matches) == 1:
            name = matches[0]
            return QueryGuess(original, name, artist=SONGS.get(name, ""))
        if len(matches) > 1:
            return QueryGuess(original, text, tuple(matches[:3]))
        # Correct an artist/title inside a longer request, preserving the rest.
        for word in sorted(words[:len(SONGS) + len(ARTISTS)], key=len, reverse=True):
            width = len(word)
            for start in range(len(text) - width + 1):
                part = text[start:start + width]
                if part != word and self.phonetic(part) == self.phonetic(word):
                    text = text[:start] + word + text[start + width:]
        artist = next((name for name in ARTISTS if name in text), "")
        if artist and text[len(artist):].startswith("的"):
            text = artist + text[len(artist) + 1:]
        return QueryGuess(original, text, artist=artist)

    def rank(self, query, results, artist=""):
        target = normalized(query)
        if artist and normalized(artist) in target:
            target = target.replace(normalized(artist), "", 1).lstrip("的")
        sound = self.phonetic(target)
        ranked = []
        for item in results:
            title = item.get("title", "")
            channel = item.get("uploader") or ""
            full = (title + " " + channel).lower()
            if any(word.lower() in full for word in NON_MUSIC_WORDS) and not any(
                    word in full for word in ('music video', 'official audio', 'lyrics', '歌词', '歌詞', '- topic', ' mv', '演唱', '歌曲', '翻唱')):
                continue
            title_key = normalized(title)
            phonetic_title = self.phonetic(title_key)
            exact = target in title_key
            phonetic_match = any(phonetic_title[i:i + len(sound)] == sound
                                 for i in range(max(0, len(phonetic_title) - len(sound) + 1))) if sound else False
            if not exact and not phonetic_match:
                continue
            score = 8 if exact else 5
            score += min(6, 2 * sum(word in full for word in MUSIC_WORDS))
            if artist and normalized(artist) in normalized(title):
                score += 2
            if artist and normalized(artist) in normalized(channel):
                score += 7
            if 'official' in channel.lower() or '官方' in channel:
                score += 3
            if channel.lower().endswith("- topic"):
                score += 4
            if any(word in full for word in ('cover', '翻唱')) and not any(
                    word in query.lower() for word in ('cover', '翻唱')):
                score -= 4
            duration = item.get("duration")
            if duration is not None and duration < 45:
                score -= 8
            if duration is not None and duration > 1800:
                score -= 4
            ranked.append((score, item))
        return sorted(ranked, key=lambda row: row[0], reverse=True)
