import unittest
from types import SimpleNamespace
from unittest.mock import Mock

try:
    import numpy as np
except ImportError:
    np = None

from bot_app.infrastructure.media.speech import SenseVoice


@unittest.skipIf(np is None, 'optional numpy not installed')
class SignalTests(unittest.TestCase):
    def setUp(self):
        self.engine = object.__new__(SenseVoice)
        self.engine.np = np

    def test_preserves_48khz_until_native_bandlimited_resampler(self):
        mono = (np.sin(np.arange(48000) * 2 * np.pi * 1000 / 48000) * 10000).astype('<i2')
        pcm = np.column_stack((mono, mono)).astype('<i2').tobytes()
        samples = self.engine.samples(pcm)
        self.assertEqual(len(samples), 48000)
        stream = Mock(result=SimpleNamespace(text='test'))
        self.engine.recognizer = Mock()
        self.engine.recognizer.create_stream.return_value = stream
        self.assertEqual(self.engine._transcribe(samples), 'test')
        sample_rate, padded = stream.accept_waveform.call_args.args
        self.assertEqual(sample_rate, 48000)
        self.assertEqual(len(padded), 60000)
        self.assertTrue(np.all(padded[:12000] == 0))

    def test_opposite_polarity_stereo_does_not_cancel_voice(self):
        mono = (np.sin(np.arange(4800) * 2 * np.pi * 1000 / 48000) * 10000).astype('<i2')
        pcm = np.column_stack((mono, -mono)).astype('<i2').tobytes()
        samples = self.engine.samples(pcm)
        self.assertGreater(float(np.sqrt(np.mean(samples ** 2))), 0.2)
