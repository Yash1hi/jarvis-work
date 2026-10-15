"""Local "hey jarvis" detection with openWakeWord. Nothing leaves the Mac until it fires."""
import time

import numpy as np
from openwakeword.model import Model
from openwakeword.utils import download_models

FRAME = 1280  # 80 ms at 16 kHz, what the model expects


class WakeWord:
    def __init__(self, threshold=0.35, cooldown=2.0, vad=0.5):
        download_models(["hey_jarvis"])  # skipped once the model is on disk
        # vad: openWakeWord's Silero speech check. A score only counts while someone is actually speaking,
        # which keeps a lower threshold (more of Yash's "Hey Jarvis"es) from firing on music or the TV.
        self.model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx", vad_threshold=vad)
        self.threshold, self.cooldown = threshold, cooldown
        self.buf = np.zeros(0, np.int16)
        self.last_hit = 0.0

    def feed(self, samples):
        """Feed 16 kHz int16 samples. Returns (fired, peak score over this chunk)."""
        self.buf = np.concatenate([self.buf, samples])
        fired, peak = False, 0.0
        while len(self.buf) >= FRAME:
            frame, self.buf = self.buf[:FRAME], self.buf[FRAME:]
            score = float(self.model.predict(frame)["hey_jarvis"])
            peak = max(peak, score)
            if score >= self.threshold and time.monotonic() - self.last_hit > self.cooldown:
                self.last_hit, fired = time.monotonic(), True
        return fired, peak

    def reset(self):
        """Forget recent audio, e.g. after a session, so the tail of it can't re-trigger."""
        self.model.reset()
        self.buf = np.zeros(0, np.int16)
