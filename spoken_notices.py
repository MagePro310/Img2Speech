"""Cached Vietnamese spoken status notices shared by device and web adapters."""

import math
import struct
import threading

from read_aloud import PCM_RATE, stream_tts


NOTICE_TEXTS = {
    "capture_start": "Đang chụp ảnh.",
    "image_processing": "Đang xử lý ảnh.",
    "read_start": "Bắt đầu đọc.",
    "read_resume": "Tiếp tục đọc.",
    "read_done": "Đã đọc xong.",
    "summary_start": "Bắt đầu tóm tắt.",
    "summary_done": "Tóm tắt xong.",
    "record_start": "Bắt đầu ghi câu hỏi.",
    "record_stop": "Đã ghi xong. Đang xử lý câu hỏi.",
    "answer_start": "Bắt đầu trả lời.",
    "answer_done": "Đã trả lời xong.",
    "no_image": "Vui lòng chụp ảnh trước.",
    "no_content": "Chưa có nội dung để thực hiện.",
    "question_wait": "Đang xử lý câu hỏi. Vui lòng chờ.",
    "camera_error": "Không chụp được ảnh. Vui lòng thử lại.",
    "microphone_error": "Không ghi được câu hỏi. Vui lòng thử lại.",
    "model_error": "Không xử lý được nội dung. Vui lòng thử lại.",
    "playback_error": "Không phát được âm thanh. Vui lòng kiểm tra loa.",
    "generic_error": "Đã xảy ra lỗi. Vui lòng thử lại.",
}

ERROR_EVENTS = {
    "camera_error", "microphone_error", "model_error",
    "playback_error", "generic_error",
}


def beep_pcm(count=1, frequency=880, duration=0.09):
    parts = []
    tone_samples = int(PCM_RATE * duration)
    gap = b"\0\0" * int(PCM_RATE * 0.06)
    for index in range(count):
        tone = bytearray()
        for sample in range(tone_samples):
            value = int(
                7000 * math.sin(2 * math.pi * frequency * sample / PCM_RATE)
            )
            tone.extend(struct.pack("<h", value))
        parts.append(bytes(tone))
        if index + 1 < count:
            parts.append(gap)
    return b"".join(parts)


class SpokenNoticeCache:
    """Generate each (model, voice, phrase) once, deduplicating concurrent calls."""

    def __init__(self, client):
        self.client = client
        self._cache = {}
        self._pending = {}
        self._lock = threading.Lock()

    def text_pcm(self, model, voice, text):
        key = (model, voice, text)
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            pending = self._pending.get(key)
            if pending is None:
                pending = threading.Event()
                self._pending[key] = pending
                creator = True
            else:
                creator = False

        if not creator:
            pending.wait()
            with self._lock:
                cached = self._cache.get(key)
            if cached is None:
                raise RuntimeError("Spoken notice generation failed.")
            return cached

        try:
            pcm = b"".join(stream_tts(self.client, model, voice, text))
            if not pcm:
                raise RuntimeError("Spoken notice TTS returned no audio.")
            with self._lock:
                self._cache[key] = pcm
            return pcm
        finally:
            with self._lock:
                self._pending.pop(key, None)
                pending.set()

    def event_pcm(self, model, voice, event):
        try:
            text = NOTICE_TEXTS[event]
        except KeyError:
            raise ValueError(f"Unknown spoken notice event: {event}")
        speech = self.text_pcm(model, voice, text)
        if event == "record_start":
            return speech + beep_pcm(1)
        if event == "record_stop":
            return beep_pcm(2) + speech
        if event in ERROR_EVENTS:
            return beep_pcm(3, 330) + speech
        return speech

    def safe_event_pcm(self, model, voice, event):
        try:
            return self.event_pcm(model, voice, event)
        except Exception:
            return beep_pcm(3, 330)
