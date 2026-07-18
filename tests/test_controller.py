import io
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import device_reader
import reader_controller
import serve_reader


def wait_for_ocr(session, timeout=1):
    deadline = time.time() + timeout
    with session.condition:
        while not session.ocr_done and time.time() < deadline:
            session.condition.wait(0.02)
    if not session.ocr_done:
        raise AssertionError("OCR worker did not finish")


class ReaderControllerTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.controller = reader_controller.ReaderController(self.client)

    def tearDown(self):
        self.controller.close()

    def load_sentences(self, deltas=("Câu một. ", "Câu hai.")):
        with patch("reader_controller.stream_ocr", return_value=iter(deltas)):
            task = self.controller.load_image(Path("sample_input.jpg"))
            wait_for_ocr(self.controller.session)
        return task

    def test_interrupt_summary_and_resume_replays_interrupted_sentence(self):
        task = self.load_sentences()

        def fake_tts(client, model, voice, text):
            yield ("audio:" + text).encode()

        with (
            patch("reader_controller.stream_tts", side_effect=fake_tts),
            patch("reader_controller.summarize_text", return_value="Tóm tắt") as summarize,
        ):
            reading = self.controller.iter_audio(task)
            self.assertEqual(next(reading), "audio:Câu một.".encode())
            summary_task = self.controller.button2()
            reading.close()

            self.assertEqual(self.controller.session.cursor, 0)
            self.assertEqual(summarize.call_args.args[2], "Câu một.")
            list(self.controller.iter_audio(summary_task))
            self.assertEqual(self.controller.state, reader_controller.ReaderState.PAUSED)

            resumed = self.controller.button1()
            chunks = list(self.controller.iter_audio(resumed))

        self.assertEqual(chunks, ["audio:Câu một.".encode(), "audio:Câu hai.".encode()])
        self.assertEqual(self.controller.session.cursor, 2)
        self.assertEqual(self.controller.state, reader_controller.ReaderState.FINISHED)

    def test_repeated_summary_uses_all_original_text_not_old_summary(self):
        task = self.load_sentences()
        with patch("reader_controller.stream_tts", return_value=iter([b"pcm"])):
            list(self.controller.iter_audio(task))
        sources = []

        def summarize(client, model, source):
            sources.append(source)
            return f"summary-{len(sources)}"

        with patch("reader_controller.summarize_text", side_effect=summarize):
            first = self.controller.button2()
            second = self.controller.button2()

        self.assertTrue(first.cancel_event.is_set())
        self.assertEqual(sources, ["Câu một.\n\nCâu hai."] * 2)
        self.assertEqual(second.text, "summary-2")

    def test_answer_requests_are_independent_without_history(self):
        self.client.chat.completions.create.side_effect = [
            SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="Trả lời một"))]),
            SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="Trả lời hai"))]),
        ]
        reader_controller.answer_question(self.client, "qa", "Nguồn", "Câu hỏi một")
        reader_controller.answer_question(self.client, "qa", "Nguồn", "Câu hỏi hai")

        second_messages = self.client.chat.completions.create.call_args_list[1].kwargs["messages"]
        self.assertEqual(len(second_messages), 2)
        self.assertIn("Câu hỏi hai", second_messages[1]["content"])
        self.assertNotIn("Câu hỏi một", second_messages[1]["content"])
        self.assertNotIn("Trả lời một", str(second_messages))

    def test_new_image_resets_latest_question_and_answer(self):
        self.load_sentences()
        self.controller.session.latest_question = "Cũ"
        self.controller.session.latest_answer = "Cũ"
        with patch("reader_controller.stream_ocr", return_value=iter(["Ảnh mới."])):
            self.controller.load_image(Path("sample_input.jpg"))
            wait_for_ocr(self.controller.session)
        self.assertIsNone(self.controller.session.latest_question)
        self.assertIsNone(self.controller.session.latest_answer)
        self.assertEqual(self.controller.session.cursor, 0)

    def test_stale_question_result_cannot_override_resumed_reading(self):
        self.load_sentences()
        session = self.controller.session
        session.started_index = 0
        self.controller.state = reader_controller.ReaderState.RECORDING
        started = threading.Event()
        release = threading.Event()
        result = []

        def slow_transcribe(*args):
            started.set()
            release.wait(1)
            return "Câu hỏi"

        with tempfile.NamedTemporaryFile(suffix=".wav") as audio, \
                patch("reader_controller.transcribe_question", side_effect=slow_transcribe), \
                patch("reader_controller.answer_question", return_value="Câu trả lời"):
            thread = threading.Thread(
                target=lambda: result.append(self.controller.button3_finish(audio.name))
            )
            thread.start()
            self.assertTrue(started.wait(1))
            resume_task = self.controller.button1()
            release.set()
            thread.join(1)

        self.assertEqual(result, [None])
        self.assertEqual(self.controller.state, reader_controller.ReaderState.READING)
        self.assertEqual(self.controller.current_task, resume_task)


class DeviceHelpersTests(unittest.TestCase):
    def test_command_requires_output_placeholder(self):
        with self.assertRaises(reader_controller.ReaderError):
            device_reader.command_args("camera --fixed", Path("x.jpg"))
        self.assertEqual(
            device_reader.command_args("camera --out {output}", Path("x.jpg")),
            ["camera", "--out", "x.jpg"],
        )

    def test_capture_validates_created_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "photo.jpg"

            def create_file(*args, **kwargs):
                output.write_bytes(b"\xff\xd8jpeg")
                return SimpleNamespace(returncode=0)

            with patch("device_reader.subprocess.run", side_effect=create_file) as run:
                self.assertEqual(
                    device_reader.capture_image("camera {output}", output), output
                )
            self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_recorder_stops_with_sigint(self):
        process = MagicMock()
        process.poll.return_value = None
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "question.wav"
            with patch("device_reader.subprocess.Popen", return_value=process):
                recorder = device_reader.Recorder("record {output}")
                recorder.start(output)
                output.write_bytes(b"RIFF\x00\x00\x00\x00WAVEdata")
                self.assertEqual(recorder.stop(), output)
        process.send_signal.assert_called_once_with(device_reader.signal.SIGINT)

    def test_beep_pcm_is_16_bit_audio(self):
        audio = device_reader.beep_pcm(2)
        self.assertGreater(len(audio), 0)
        self.assertEqual(len(audio) % 2, 0)


class WebAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        serve_reader.ReaderHandler.cfg = SimpleNamespace(
            ocr_model="ocr", tts_model="tts", summary_model="summary",
            stt_model="stt", qa_model="qa", voice="voice",
        )
        serve_reader.ReaderHandler.oai_client = MagicMock()

    def tearDown(self):
        with serve_reader.ReaderHandler.session_lock:
            sessions = list(serve_reader.ReaderHandler.sessions.values())
            serve_reader.ReaderHandler.sessions.clear()
        for session in sessions:
            session.close()

    def test_page_has_three_buttons_camera_and_media_recorder(self):
        self.assertIn('id="b1"', serve_reader.PAGE)
        self.assertIn('id="b2"', serve_reader.PAGE)
        self.assertIn('id="b3"', serve_reader.PAGE)
        self.assertIn('capture="environment"', serve_reader.PAGE)
        self.assertIn("image/png", serve_reader.PAGE)
        self.assertIn("MediaRecorder", serve_reader.PAGE)
        self.assertIn("/button/3", serve_reader.PAGE)

    def test_web_upload_accepts_jpeg_and_png_signatures(self):
        self.assertEqual(
            serve_reader.image_suffix("image/jpeg", b"\xff\xd8jpeg"), ".jpg"
        )
        self.assertEqual(
            serve_reader.image_suffix("image/png", b"\x89PNG\r\n\x1a\npng"), ".png"
        )
        with self.assertRaises(reader_controller.ReaderError):
            serve_reader.image_suffix("image/png", b"not-png")
        with self.assertRaises(reader_controller.ReaderError):
            serve_reader.image_suffix("image/gif", b"GIF89a")

    def test_session_cache_is_limited(self):
        first = serve_reader.ReaderHandler.create_session("session00")
        first.close = MagicMock(wraps=first.close)
        for index in range(1, serve_reader.MAX_SESSIONS + 1):
            serve_reader.ReaderHandler.create_session(f"session{index:02d}")
        self.assertEqual(len(serve_reader.ReaderHandler.sessions), serve_reader.MAX_SESSIONS)
        first.close.assert_called_once()

    def test_response_contains_only_latest_question_and_answer(self):
        session = serve_reader.ReaderHandler.create_session("session01")
        session.controller.session = reader_controller.DocumentSession(Path("x.jpg"))
        session.controller.session.latest_question = "Mới nhất"
        session.controller.session.latest_answer = "Trả lời mới nhất"
        data = serve_reader.ReaderHandler.response_for(session)
        self.assertEqual(data["question"], "Mới nhất")
        self.assertEqual(data["answer"], "Trả lời mới nhất")
        self.assertNotIn("history", data)

    def test_upload_limit_is_enforced(self):
        handler = object.__new__(serve_reader.ReaderHandler)
        handler.headers = MagicMock()
        handler.headers.get.return_value = str(serve_reader.MAX_UPLOAD + 1)
        handler.rfile = io.BytesIO()
        with self.assertRaises(reader_controller.ReaderError):
            handler.read_body()


if __name__ == "__main__":
    unittest.main()
