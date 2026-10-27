"""Local SenseVoice inference, called only on a dedicated worker thread."""
from pathlib import Path
import audioop
import time
import tempfile

from bot_app.domain.voice_commands import SpeechResult


class SenseVoice:
    def __init__(self, model_dir, wake_model_dir=None, alternate_model_dir=None, language='zh'):
        import numpy as np
        import sherpa_onnx
        self.np = np
        self.sherpa = sherpa_onnx
        self.alternate_model_dir = Path(alternate_model_dir) if alternate_model_dir else None
        self.alternate = None
        folder = Path(model_dir)
        self.model_name = folder.name
        self.language = language
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(folder / "model.int8.onnx"), tokens=str(folder / "tokens.txt"),
            num_threads=2, use_itn=True, language=language, debug=False,
        )
        self.spotter = None
        self.wake_streams = {}
        # Two complete names in sound, independent of their ASR spelling.
        self.keywords = "/".join(
            f"m iāo j {first} m iāo j {second} @wake{a}{b}"
            for a, first in enumerate(("iàng", "iāng"))
            for b, second in enumerate(("iàng", "iāng")))
        if wake_model_dir:
            wake = Path(wake_model_dir)
            suffix = "epoch-13-avg-2-chunk-16-left-64"
            with tempfile.TemporaryDirectory(prefix="miaowake-") as temporary:
                keywords = Path(temporary) / "keywords.txt"
                keywords.write_text(self.keywords.replace("/", "\n") + "\n", encoding="utf-8")
                self.spotter = sherpa_onnx.KeywordSpotter(
                    tokens=str(wake / "tokens.txt"),
                    encoder=str(wake / f"encoder-{suffix}.int8.onnx"),
                    decoder=str(wake / f"decoder-{suffix}.onnx"),
                    joiner=str(wake / f"joiner-{suffix}.int8.onnx"),
                    keywords_file=str(keywords), num_threads=1, keywords_score=2,
                    keywords_threshold=0.2, num_trailing_blanks=1,
                )

    def samples(self, pcm):
        stereo = self.np.frombuffer(pcm, dtype="<i2").reshape(-1, 2).astype(self.np.float32) / 32768
        mono = stereo.mean(axis=1)
        channel_rms = self.np.sqrt(self.np.mean(stereo * stereo, axis=0))
        mono_rms = self.np.sqrt(self.np.mean(mono * mono))
        if mono_rms < channel_rms.max() * 0.25:
            mono = stereo[:, int(channel_rms.argmax())]
        # Keep native 48 kHz. The recognizer performs proper band-limited
        # resampling; plain ratecv decimation can alias wide-band microphone
        # audio and synthetic 16 kHz fixtures do not reveal that distortion.
        return mono

    def heard_wake(self, samples, speaker_key):
        if self.spotter is None:
            return False
        now = time.monotonic()
        self.wake_streams = {key: value for key, value in self.wake_streams.items()
                             if now - value[1] < 3 and value[2] < 48000 * 20}
        if speaker_key not in self.wake_streams:
            if len(self.wake_streams) >= 64:
                self.wake_streams.clear()
            self.wake_streams[speaker_key] = (self.spotter.create_stream(), now, 0)
        stream, _, count = self.wake_streams[speaker_key]
        stream.accept_waveform(48000, samples)
        # Discord stops sending packets during silence. Supply the lookahead
        # needed by the streaming keyword model without ending a split wake.
        stream.accept_waveform(48000, self.np.zeros(31680, dtype=self.np.float32))
        found = False
        while self.spotter.is_ready(stream):
            self.spotter.decode_stream(stream)
            if self.spotter.get_result(stream):
                found = True
                self.spotter.reset_stream(stream)
        self.wake_streams[speaker_key] = (stream, now, count + len(samples) + 31680)
        return found

    def recognize(self, pcm, speaker_key):
        samples = self.samples(pcm)
        wake = self.heard_wake(samples, speaker_key)
        return SpeechResult(self._transcribe(samples), wake)

    def recognize_diagnostic(self, pcm, speaker_key):
        t = time.monotonic()
        result = self.recognize(pcm, speaker_key)
        primary_seconds = time.monotonic() - t
        alternate_text = ""
        if self.alternate_model_dir and self.alternate is None:
            self.alternate = self.sherpa.OfflineRecognizer.from_paraformer(
                paraformer=str(self.alternate_model_dir / "model.int8.onnx"),
                tokens=str(self.alternate_model_dir / "tokens.txt"), num_threads=2, debug=False)
        if self.alternate:
            stream = self.alternate.create_stream()
            samples = self.samples(pcm)
            stream.accept_waveform(48000, self.np.concatenate((self.np.zeros(12000, dtype=self.np.float32), samples)))
            self.alternate.decode_stream(stream)
            alternate_text = stream.result.text
        stereo = self.np.frombuffer(pcm, dtype="<i2").reshape(-1, 2).astype(self.np.float32)
        left, right = stereo[:, 0], stereo[:, 1]
        diagnostics = {'audio_seconds': round(len(pcm) / 192000, 2),
            'mono_rms': round(float(self.np.sqrt(self.np.mean(self.samples(pcm) ** 2))) * 32768),
            'stereo_correlation': round(float(self.np.mean(left * right) /
                max(1, self.np.sqrt(self.np.mean(left ** 2) * self.np.mean(right ** 2)))), 3),
            'asr_seconds': round(primary_seconds, 2)}
        return SpeechResult(result.text, result.heard_wake, alternate_text, diagnostics)

    def transcribe(self, pcm):
        return self._transcribe(self.samples(pcm))

    def _transcribe(self, samples):
        # Discord's DTX starts a segment at the first voiced frame. SenseVoice
        # can omit the opening syllable at that edge. 250 ms of leading silence
        # restored it in the reproduced play/resume audio fixtures.
        samples = self.np.concatenate((self.np.zeros(12000, dtype=self.np.float32), samples))
        stream = self.recognizer.create_stream()
        stream.accept_waveform(48000, samples)
        self.recognizer.decode_stream(stream)
        return stream.result.text
