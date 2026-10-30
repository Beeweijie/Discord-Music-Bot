"""Pronunciation dictionary adapter; YouTube is searched with music intent."""
from functools import lru_cache
from pypinyin import lazy_pinyin
from bot_app.features.music.phonetics import MusicAssociator


@lru_cache(maxsize=4096)
def pronunciation(text):
    return tuple(lazy_pinyin(text))


class VoiceMusicSearch:
    def __init__(self):
        self.associator = MusicAssociator(pronunciation)

    def resolve(self, query, titles, search):
        guess = self.associator.guess(query, titles)
        names = guess.alternatives or (guess.query,)
        candidates = []
        for name in names:
            ranked = self.associator.rank(name, search(name + " " + guess.artist + " 歌曲"), guess.artist)
            for score, item in ranked[:3]:
                if item.get("url") and all(old["url"] != item["url"] for old in candidates):
                    candidates.append(dict(item, score=score, associated_name=name))
        candidates.sort(key=lambda item: item['score'], reverse=True)
        selected = candidates[0] if candidates and candidates[0]['score'] >= 10 and not guess.alternatives else None
        return guess, candidates[:3], selected
