import unittest

try:
    from bot_app.infrastructure.media.voice_music import VoiceMusicSearch, pronunciation
except ImportError:
    VoiceMusicSearch = None
from bot_app.domain.voice_music import MusicAssociator


@unittest.skipIf(VoiceMusicSearch is None, 'optional pypinyin dependency not installed')
class AssociationTests(unittest.TestCase):
    def setUp(self):
        self.associator = MusicAssociator(pronunciation)

    def test_same_sound_song_correction_and_polite_queue_request(self):
        for text in ['与爱', '雨爱吧', '與愛', '雨愛']:
            result = self.associator.guess(text)
            self.assertEqual(result.query, '雨爱')
            self.assertEqual(result.artist, '杨丞琳')
        self.assertEqual(self.associator.guess('清天').query, '晴天')

    def test_artist_and_song_inside_longer_request(self):
        result = self.associator.guess('周结伦的清天')
        self.assertEqual(result.query, '周杰伦晴天')
        self.assertEqual(result.artist, '周杰伦')

    def test_server_library_extends_dictionary(self):
        result = self.associator.guess('巡保', ['群星 - 寻宝 Official Music Video'])
        self.assertEqual(result.query, '寻宝')

    def test_ambiguous_homophones_return_candidates(self):
        result = self.associator.guess('欣宇', ['歌手 心语 MV', '新雨 Official Audio'])
        self.assertGreater(len(result.alternatives), 1)

    def test_english_names_keep_spaces(self):
        self.assertEqual(self.associator.guess('Taylor Swift Love Story').query, 'Taylor Swift Love Story')

    def test_artist_and_title_can_be_separated_by_english_metadata(self):
        rows = [
            {'title': '周杰伦 - 某人 Cover - 晴天 官方歌词', 'uploader': '翻唱频道', 'duration': 240, 'url': 'cover'},
            {'title': '周杰倫 Jay Chou【晴天 Sunny Day】Official MV', 'uploader': '周杰倫 Jay Chou',
             'duration': 260, 'url': 'original'}]
        ranked = self.associator.rank('周杰伦晴天', rows, '周杰伦')
        self.assertEqual(ranked[0][1]['url'], 'original')

    def test_music_search_rejects_the_reported_lecture(self):
        results = [
            {'title': '只需一個簡單動作，就能判斷你的元神是否真正歸位！#曾仕強#人生智慧#修行',
             'uploader': '曾仕強', 'duration': 300, 'url': 'https://www.youtube.com/watch?v=lecture0001'},
            {'title': '原神 Original Soundtrack Official Audio', 'uploader': 'Music - Topic',
             'duration': 200, 'url': 'https://www.youtube.com/watch?v=music000001'},
        ]
        ranked = self.associator.rank('元神', results)
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0][1]['url'], results[1]['url'])

    def test_rain_love_correction_is_used_for_search(self):
        calls = []
        def search(query):
            calls.append(query)
            return [{'title': '楊丞琳 Rainie Yang - 雨愛 Official Music Video', 'uploader': '楊丞琳',
                     'duration': 250, 'url': 'https://www.youtube.com/watch?v=rainlove001'}]
        guess, candidates, selected = VoiceMusicSearch().resolve('与爱', [], search)
        self.assertEqual(guess.query, '雨爱')
        self.assertIn('雨爱', calls[0])
        self.assertNotIn('OST', calls[0])
        self.assertEqual(selected['url'], 'https://www.youtube.com/watch?v=rainlove001')

    def test_unrelated_result_never_selected(self):
        guess, candidates, selected = VoiceMusicSearch().resolve('雨爱', [], lambda _: [
            {'title': '人生智慧如何获得幸福', 'uploader': '讲座', 'duration': 200, 'url': 'bad'}])
        self.assertEqual(candidates, [])
        self.assertIsNone(selected)
