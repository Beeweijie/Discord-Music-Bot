import tempfile
import unittest
from pathlib import Path
from bot_app.domain.music.library import track_key, track_data
from bot_app.application.music.models import Song, ChannelSession
from bot_app.infrastructure.persistence.library import LibraryRepository
from bot_app.presentation.discord.panels import MusicPresenter


class LibraryTests(unittest.TestCase):
    def test_youtube_variants_dedupe_but_case_sensitive_ids_do_not(self):
        def track(url):
            return Song(url, "title", 1, "user", True)
        self.assertEqual(track_key(track("https://youtu.be/AbC?si=share")),
                         track_key(track("https://music.youtube.com/watch?v=AbC&list=123")))
        self.assertNotEqual(track_key(track("https://youtu.be/AbC")), track_key(track("https://youtu.be/abc")))

    def test_corrupt_library_is_retained_and_invalid_local_paths_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = LibraryRepository(tmp)
            repo.save(1, 2, {"favorites":[track_data(Song("../secret", "title", 1, "user", False))], "playlists":{}})
            with self.assertRaisesRegex(ValueError, "damaged"):
                repo.read(1, 2)
            self.assertTrue(repo.path(1, 2).exists())
            with self.assertRaises(ValueError):
                repo.read("../other", 2)
            self.assertEqual(repo.read(1, 3)["favorites"], [])

    def test_queue_page_fits_discord_even_with_long_titles(self):
        presenter = MusicPresenter(None)
        session = ChannelSession(queue=[Song(str(i), "*"*300, i, "*"*100, False) for i in range(500)])
        text = presenter.queue_text(session, 50)
        self.assertIn("491.", text)
        self.assertIn("500.", text)
        self.assertLessEqual(len(text), 2000)
        with self.assertRaises(ValueError):
            presenter.queue_text(session, 51)
