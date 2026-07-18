import queue
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import read_aloud


class SummaryPipelineTests(unittest.TestCase):
    def test_segment_worker_returns_full_transcript_and_preserves_segments(self):
        segments = queue.Queue()
        transcript = queue.Queue()
        deltas = ["Câu thứ nhất. ", "Câu thứ hai."]

        with patch("read_aloud.stream_ocr", return_value=iter(deltas)):
            read_aloud.segment_worker(
                object(), "ocr-model", object(), segments, time.perf_counter(), transcript
            )

        self.assertEqual(transcript.get_nowait(), "Câu thứ nhất. Câu thứ hai.")
        self.assertEqual(segments.get_nowait(), "Câu thứ nhất.")
        self.assertEqual(segments.get_nowait(), "Câu thứ hai.")
        self.assertIsNone(segments.get_nowait())

    def test_summarize_text_uses_requested_model_and_full_text(self):
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Ý một. Ý hai."))]
        )

        result = read_aloud.summarize_text(client, "summary-model", "Toàn bộ transcript")

        self.assertEqual(result, "Ý một. Ý hai.")
        kwargs = client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["model"], "summary-model")
        self.assertEqual(kwargs["messages"][1]["content"], "Toàn bộ transcript")
        self.assertIn("3 đến 5 ý", kwargs["messages"][0]["content"])
        self.assertIn("tiếng Việt", kwargs["messages"][0]["content"])
        self.assertIn("không có trong nội dung", kwargs["messages"][0]["content"])

    def test_spoken_tracker_uses_completed_boundaries_and_first_fallback(self):
        tracker = read_aloud.SpokenTracker()
        tracker.start_segment("Câu thứ nhất.")
        tracker.finish_segment("Câu thứ nhất.", read_aloud.PCM_BYTES_PER_SEC)
        tracker.start_segment("Câu thứ hai.")
        tracker.finish_segment("Câu thứ hai.", read_aloud.PCM_BYTES_PER_SEC)

        self.assertEqual(tracker.text_for_seconds(0.25), "Câu thứ nhất.")
        self.assertEqual(tracker.text_for_seconds(1.0), "Câu thứ nhất.")
        self.assertEqual(tracker.text_for_seconds(1.99), "Câu thứ nhất.")
        self.assertEqual(
            tracker.text_for_seconds(2.0), "Câu thứ nhất.\n\nCâu thứ hai."
        )

    def test_gpio_button_ignores_press_until_playback_is_armed(self):
        events = []
        armed = threading.Event()

        class FakeButton:
            def __init__(self, pin, pull_up, bounce_time):
                events.append(("init", pin, pull_up, bounce_time))

            def close(self):
                events.append(("close",))

        button = read_aloud.setup_summary_button(
            17, lambda: events.append(("press",)), armed, button_class=FakeButton
        )
        button.when_pressed()
        armed.set()
        button.when_pressed()
        button.close()

        self.assertEqual(events, [
            ("init", 17, True, 0.1),
            ("press",),
            ("close",),
        ])

    def test_playback_interrupt_kills_player_and_keeps_first_segment_fallback(self):
        audio_queue = queue.Queue()
        chunks = queue.Queue()
        chunks.put(b"first chunk")
        chunks.put(b"unheard chunk")
        chunks.put(None)
        audio_queue.put(("Câu đầu tiên.", chunks))
        audio_queue.put(None)
        stop = threading.Event()
        armed = threading.Event()
        cancelled = threading.Event()
        tracker = read_aloud.SpokenTracker()

        class StopSink:
            def write(self, chunk):
                stop.set()

            def flush(self):
                pass

            def close(self):
                pass

        player = MagicMock()
        result = read_aloud.play_audio_queue(
            audio_queue, StopSink(), player, stop, armed, cancelled,
            tracker, time.perf_counter()
        )

        self.assertTrue(result.interrupted)
        self.assertTrue(cancelled.is_set())
        player.kill.assert_called_once()
        self.assertEqual(tracker.completed, [])
        self.assertEqual(tracker.text_for_seconds(0), "Câu đầu tiên.")

    def test_normal_playback_marks_segment_complete_without_killing_player(self):
        audio_queue = queue.Queue()
        chunks = queue.Queue()
        chunks.put(b"complete audio")
        chunks.put(None)
        audio_queue.put(("Câu hoàn tất.", chunks))
        audio_queue.put(None)
        sink = MagicMock()
        player = MagicMock()
        player.poll.return_value = 0
        tracker = read_aloud.SpokenTracker()

        result = read_aloud.play_audio_queue(
            audio_queue, sink, player, threading.Event(), threading.Event(),
            threading.Event(), tracker, time.perf_counter()
        )

        self.assertFalse(result.interrupted)
        self.assertEqual(result.total_bytes, len(b"complete audio"))
        self.assertEqual(tracker.completed[0][0], "Câu hoàn tất.")
        player.kill.assert_not_called()
        player.wait.assert_called_once()

    def test_cancelled_tts_worker_exits_without_waiting_for_segments(self):
        cancelled = threading.Event()
        cancelled.set()
        read_aloud.tts_worker(
            object(), "tts", "voice", queue.Queue(), queue.Queue(),
            time.perf_counter(), cancelled
        )


if __name__ == "__main__":
    unittest.main()
