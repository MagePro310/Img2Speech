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
import spoken_notices
from read_aloud import DEFAULT_VOICE, PCM_BYTES_PER_SEC


class SilentNoticeCache:
    def safe_event_pcm(self, model, voice, event):
        return b""


class TaggedNoticeCache:
    def safe_event_pcm(self, model, voice, event):
        return f"notice:{event}".encode()


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
        self.controller = reader_controller.ReaderController(
            self.client, notice_cache=SilentNoticeCache()
        )

    def tearDown(self):
        self.controller.close()

    def test_default_voice_is_marin(self):
        self.assertEqual(DEFAULT_VOICE, "marin")
        self.assertEqual(self.controller.voice, DEFAULT_VOICE)

    def test_discard_image_cancels_and_forgets_the_current_page(self):
        task = self.load_sentences()
        session = self.controller.session

        self.controller.discard_image()

        self.assertTrue(task.cancel_event.is_set())
        self.assertTrue(session.ocr_cancel.is_set())
        self.assertIsNone(self.controller.session)
        self.assertIsNone(self.controller.last_reading_task)
        self.assertEqual(self.controller.state, reader_controller.ReaderState.IDLE)

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

    def test_discarded_page_suppresses_stale_model_and_audio_errors(self):
        reading_task = self.load_sentences()
        with patch("reader_controller.stream_tts", return_value=iter([b"pcm"])):
            list(self.controller.iter_audio(reading_task))

        def stale_summary(*args):
            self.controller.discard_image()
            raise RuntimeError("old summary failed")

        with patch("reader_controller.summarize_text", side_effect=stale_summary):
            self.assertIsNone(self.controller.button2())
        self.assertEqual(self.controller.state, reader_controller.ReaderState.IDLE)

        reading_task = self.load_sentences()

        def stale_audio():
            self.controller.discard_image()
            raise RuntimeError("old audio failed")
            yield  # pragma: no cover - makes this a generator

        with patch("reader_controller.stream_tts", return_value=stale_audio()):
            self.assertEqual(list(self.controller.iter_audio(reading_task)), [])
        self.assertEqual(self.controller.state, reader_controller.ReaderState.IDLE)

    def test_web_progress_rewinds_buffered_audio_and_resumes_after_answer(self):
        reading_task = self.load_sentences()

        def one_second_tts(client, model, voice, text):
            yield b"x" * PCM_BYTES_PER_SEC

        with patch("reader_controller.stream_tts", side_effect=one_second_tts):
            list(self.controller.iter_audio(reading_task))
        self.assertEqual(self.controller.session.cursor, 2)
        self.assertEqual(self.controller.state, reader_controller.ReaderState.FINISHED)

        # The HTTP producer finished both sentences, but the browser has only
        # played the first sentence plus half of the second.
        self.assertTrue(self.controller.sync_web_playback(reading_task.generation, 1.5))
        self.assertEqual(self.controller.session.cursor, 1)
        self.assertEqual(self.controller.session.started_index, 1)

        self.controller.button3_start()
        with (
            patch("reader_controller.transcribe_question", return_value="Hỏi mới"),
            patch("reader_controller.answer_question", return_value="Trả lời mới"),
            patch("reader_controller.stream_tts", side_effect=one_second_tts),
        ):
            answer_task = self.controller.button3_finish("question.webm")
            list(self.controller.iter_audio(answer_task))
            self.assertEqual(self.controller.state, reader_controller.ReaderState.PAUSED)
            self.assertTrue(self.controller.status()["can_resume"])
            resumed_task = self.controller.button1()

        self.assertEqual(resumed_task.start_cursor, 1)
        self.assertEqual(self.controller.state, reader_controller.ReaderState.READING)

    def test_web_progress_mid_first_sentence_preserves_fallback(self):
        reading_task = self.load_sentences()
        with patch(
            "reader_controller.stream_tts",
            side_effect=lambda *args: iter([b"x" * PCM_BYTES_PER_SEC]),
        ):
            list(self.controller.iter_audio(reading_task))

        self.controller.sync_web_playback(reading_task.generation, 0.5)
        self.assertEqual(self.controller.session.cursor, 0)
        self.assertEqual(self.controller.session.source_text(), "Câu một.")

    def test_stale_web_progress_does_not_change_cursor(self):
        reading_task = self.load_sentences()
        with patch("reader_controller.stream_tts", return_value=iter([b"pcm"])):
            list(self.controller.iter_audio(reading_task))
        self.assertFalse(
            self.controller.sync_web_playback(reading_task.generation - 1, 0)
        )
        self.assertEqual(self.controller.session.cursor, 2)

    def test_question_after_natural_completion_still_requires_new_image(self):
        reading_task = self.load_sentences()

        def one_second_tts(*args):
            yield b"x" * PCM_BYTES_PER_SEC

        with patch("reader_controller.stream_tts", side_effect=one_second_tts):
            list(self.controller.iter_audio(reading_task))
        self.controller.sync_web_playback(reading_task.generation, 2.0)
        self.controller.button3_start()
        with (
            patch("reader_controller.transcribe_question", return_value="Hỏi"),
            patch("reader_controller.answer_question", return_value="Đáp"),
            patch("reader_controller.stream_tts", side_effect=one_second_tts),
        ):
            answer_task = self.controller.button3_finish("question.webm")
            list(self.controller.iter_audio(answer_task))

        self.assertEqual(self.controller.state, reader_controller.ReaderState.FINISHED)
        self.assertFalse(self.controller.status()["can_resume"])
        with self.assertRaises(reader_controller.ReaderError):
            self.controller.button1()

    def test_web_progress_limits_summary_to_sentences_actually_heard(self):
        reading_task = self.load_sentences()

        def one_second_tts(*args):
            yield b"x" * PCM_BYTES_PER_SEC

        with patch("reader_controller.stream_tts", side_effect=one_second_tts):
            list(self.controller.iter_audio(reading_task))
        self.controller.sync_web_playback(reading_task.generation, 1.5)
        with patch("reader_controller.summarize_text", return_value="Tóm tắt") as summarize:
            self.controller.button2()
        self.assertEqual(summarize.call_args.args[2], "Câu một.")

    def test_reading_notices_wrap_audio_without_entering_source_text(self):
        controller = reader_controller.ReaderController(
            self.client, notice_cache=TaggedNoticeCache()
        )
        try:
            with patch("reader_controller.stream_ocr", return_value=iter(["Câu gốc."])):
                task = controller.load_image(Path("sample_input.jpg"))
                wait_for_ocr(controller.session)

            def fake_tts(client, model, voice, text):
                yield f"speech:{text}".encode()

            with patch("reader_controller.stream_tts", side_effect=fake_tts):
                chunks = list(controller.iter_audio(task))

            self.assertEqual(
                chunks,
                [b"notice:read_start", b"speech:C\xc3\xa2u g\xe1\xbb\x91c.",
                 b"notice:read_done"],
            )
            self.assertEqual(controller.session.source_text(), "Câu gốc.")
            expected_bytes = len(chunks[0]) + len(chunks[1])
            self.assertEqual(task.pcm_bytes, expected_bytes)
            self.assertEqual(
                task.timeline[0][1], expected_bytes / PCM_BYTES_PER_SEC
            )
        finally:
            controller.close()

    def test_cancelled_reading_does_not_play_done_notice(self):
        controller = reader_controller.ReaderController(
            self.client, notice_cache=TaggedNoticeCache()
        )
        try:
            with patch("reader_controller.stream_ocr", return_value=iter(["Câu gốc."])):
                task = controller.load_image(Path("sample_input.jpg"))
                wait_for_ocr(controller.session)
            with patch(
                "reader_controller.stream_tts", return_value=iter([b"part-1", b"part-2"])
            ):
                audio = controller.iter_audio(task)
                chunks = [next(audio), next(audio)]
                task.cancel_event.set()
                chunks.extend(audio)
            self.assertNotIn(b"notice:read_done", chunks)
        finally:
            controller.close()

    def test_summary_and_answer_completion_notices_are_ordered(self):
        controller = reader_controller.ReaderController(
            self.client, notice_cache=TaggedNoticeCache()
        )
        try:
            with patch("reader_controller.stream_ocr", return_value=iter(["Nguồn."])):
                reading = controller.load_image(Path("sample_input.jpg"))
                wait_for_ocr(controller.session)
            with patch("reader_controller.stream_tts", return_value=iter([b"source"])):
                list(controller.iter_audio(reading))

            with (
                patch("reader_controller.summarize_text", return_value="Nội dung tóm tắt"),
                patch(
                    "reader_controller.stream_tts",
                    side_effect=lambda client, model, voice, text: iter(
                        [f"speech:{text}".encode()]
                    ),
                ),
            ):
                summary = controller.button2()
                summary_chunks = list(controller.iter_audio(summary))
            self.assertEqual(
                summary_chunks,
                [b"speech:N\xe1\xbb\x99i dung t\xc3\xb3m t\xe1\xba\xaft",
                 b"notice:summary_done"],
            )

            controller.state = reader_controller.ReaderState.RECORDING
            with (
                patch("reader_controller.transcribe_question", return_value="Hỏi"),
                patch("reader_controller.answer_question", return_value="Đáp"),
                patch("reader_controller.stream_tts", return_value=iter([b"answer"])),
            ):
                answer = controller.button3_finish("question.wav")
                answer_chunks = list(controller.iter_audio(answer))
            self.assertEqual(
                answer_chunks,
                [b"notice:answer_start", b"answer", b"notice:answer_done"],
            )
        finally:
            controller.close()


class SpokenNoticeTests(unittest.TestCase):
    def test_cache_reuses_phrase_and_separates_voices(self):
        cache = spoken_notices.SpokenNoticeCache(MagicMock())

        def fake_tts(client, model, voice, text):
            yield f"{voice}:{text}".encode()

        with patch("spoken_notices.stream_tts", side_effect=fake_tts) as tts:
            first = cache.event_pcm("tts", "onyx", "read_start")
            second = cache.event_pcm("tts", "onyx", "read_start")
            other_voice = cache.event_pcm("tts", "alloy", "read_start")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other_voice)
        self.assertEqual(tts.call_count, 2)

    def test_concurrent_requests_generate_notice_once(self):
        cache = spoken_notices.SpokenNoticeCache(MagicMock())
        started = threading.Event()
        release = threading.Event()
        results = []

        def slow_tts(*args):
            started.set()
            release.wait(1)
            yield b"pcm"

        with patch("spoken_notices.stream_tts", side_effect=slow_tts) as tts:
            first = threading.Thread(
                target=lambda: results.append(cache.event_pcm("tts", "onyx", "read_done"))
            )
            second = threading.Thread(
                target=lambda: results.append(cache.event_pcm("tts", "onyx", "read_done"))
            )
            first.start()
            self.assertTrue(started.wait(1))
            second.start()
            release.set()
            first.join(1)
            second.join(1)
        self.assertEqual(results, [b"pcm", b"pcm"])
        self.assertEqual(tts.call_count, 1)

    def test_failed_notice_falls_back_to_three_low_beeps(self):
        cache = spoken_notices.SpokenNoticeCache(MagicMock())
        with patch("spoken_notices.stream_tts", side_effect=RuntimeError("offline")):
            pcm = cache.safe_event_pcm("tts", "onyx", "read_start")
        self.assertEqual(pcm, spoken_notices.beep_pcm(3, 330))

    def test_recording_beeps_have_the_required_order(self):
        cache = spoken_notices.SpokenNoticeCache(MagicMock())
        with patch("spoken_notices.stream_tts", return_value=iter([b"speech"])):
            start = cache.event_pcm("tts", "onyx", "record_start")
        with patch("spoken_notices.stream_tts", return_value=iter([b"speech"])):
            stop = cache.event_pcm("tts", "onyx", "record_stop")
        self.assertTrue(start.startswith(b"speech"))
        self.assertTrue(start.endswith(spoken_notices.beep_pcm(1)))
        self.assertTrue(stop.startswith(spoken_notices.beep_pcm(2)))
        self.assertTrue(stop.endswith(b"speech"))


class DeviceHelpersTests(unittest.TestCase):
    def test_button1_short_and_long_press_emit_one_action_each(self):
        events = queue.Queue()
        button = SimpleNamespace()
        device_reader.configure_button1_gestures(button, events)

        self.assertEqual(
            button.hold_time, device_reader.BUTTON1_NEW_IMAGE_HOLD_SECONDS
        )
        self.assertFalse(button.hold_repeat)

        button.when_pressed()
        button.when_released()
        self.assertEqual(events.get_nowait(), ("button", 1))
        button.when_held()  # A late hold callback must not add a second action.
        with self.assertRaises(queue.Empty):
            events.get_nowait()

        button.when_pressed()
        button.when_held()
        button.when_held()  # hold_repeat is false, but also guard duplicate callbacks.
        button.when_released()
        self.assertEqual(events.get_nowait(), ("new_image",))
        with self.assertRaises(queue.Empty):
            events.get_nowait()

    def test_device_dispatches_force_new_image_and_ignores_stale_record_timeout(self):
        reader = object.__new__(device_reader.DeviceReader)
        reader.handle_button1 = MagicMock()
        reader.handle_button2 = MagicMock()
        reader.handle_button3 = MagicMock()
        reader.new_image = MagicMock()
        reader.controller = SimpleNamespace(state=reader_controller.ReaderState.RECORDING)
        reader.recording_token = 3

        reader.dispatch_event(("new_image",))
        reader.new_image.assert_called_once_with()

        reader.dispatch_event(("record_timeout", 2))
        reader.handle_button3.assert_not_called()
        reader.dispatch_event(("record_timeout", 3))
        reader.handle_button3.assert_called_once_with()

    def test_default_gpio_mapping_can_be_overridden(self):
        required = [
            "--capture-command", "camera {output}",
            "--record-command", "record {output}",
        ]
        defaults = device_reader.build_parser().parse_args(required)
        self.assertEqual(
            (defaults.button1_pin, defaults.button2_pin, defaults.button3_pin),
            (17, 27, 22),
        )

        custom = device_reader.build_parser().parse_args([
            *required,
            "--button1-pin", "5",
            "--button2-pin", "6",
            "--button3-pin", "13",
        ])
        self.assertEqual(
            (custom.button1_pin, custom.button2_pin, custom.button3_pin),
            (5, 6, 13),
        )

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

    def test_recording_notices_surround_recorder_actions(self):
        reader = object.__new__(device_reader.DeviceReader)
        reader.controller = MagicMock()
        reader.controller.state = reader_controller.ReaderState.PAUSED
        reader.controller.button3_start.return_value = True
        reader.recorder = MagicMock()
        reader.args = SimpleNamespace(max_record_seconds=60)
        reader.workdir = SimpleNamespace(name=tempfile.gettempdir())
        reader.record_timer = None
        reader.recording_token = 0
        order = []
        reader.stop_player = lambda: order.append("stop_player")
        reader.speak_notice = lambda event: order.append(event)
        reader.recorder.start.side_effect = lambda path: order.append("recorder_start")

        timer = MagicMock()
        with patch("device_reader.threading.Timer", return_value=timer):
            reader.handle_button3()
        self.assertLess(order.index("record_start"), order.index("recorder_start"))

        order.clear()
        reader.controller.state = reader_controller.ReaderState.RECORDING
        reader.record_timer = timer
        reader.recorder.stop.side_effect = (
            lambda discard=False: order.append("recorder_stop") or Path("q.wav")
        )
        reader.async_call = lambda label, fn: order.append("start_stt")
        reader.handle_button3()
        self.assertEqual(order, ["recorder_stop", "record_stop", "start_stt"])


class WebAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        serve_reader.ReaderHandler.cfg = SimpleNamespace(
            ocr_model="ocr", tts_model="tts", summary_model="summary",
            stt_model="stt", qa_model="qa", voice="voice",
        )
        serve_reader.ReaderHandler.oai_client = MagicMock()
        serve_reader.ReaderHandler.notice_cache = SilentNoticeCache()

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
        self.assertIn("played_seconds", serve_reader.PAGE)
        self.assertIn("canResume", serve_reader.PAGE)
        self.assertIn('id="noticeAudio"', serve_reader.PAGE)
        self.assertIn("/notice?event=", serve_reader.PAGE)
        self.assertLess(
            serve_reader.PAGE.index("playNotice('record_start')"),
            serve_reader.PAGE.index("new MediaRecorder"),
        )

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

    def test_playback_progress_query_is_optional_and_validated(self):
        self.assertIsNone(serve_reader.playback_progress({}))
        self.assertEqual(
            serve_reader.playback_progress(
                {"generation": ["12"], "played_seconds": ["1.25"]}
            ),
            (12, 1.25),
        )
        with self.assertRaises(reader_controller.ReaderError):
            serve_reader.playback_progress({"generation": ["12"]})

    def test_notice_endpoint_is_whitelisted_and_streams_wav(self):
        handler = object.__new__(serve_reader.ReaderHandler)
        handler.cfg = serve_reader.ReaderHandler.cfg
        handler.notice_cache = SilentNoticeCache()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()
        handler.error_json = MagicMock()
        handler.wfile = io.BytesIO()
        handler.stream_notice("read_start")
        handler.send_response.assert_called_once_with(200)
        self.assertTrue(handler.wfile.getvalue().startswith(b"RIFF"))

        handler.error_json.reset_mock()
        handler.stream_notice("arbitrary text")
        handler.error_json.assert_called_once_with(400, "unknown spoken notice event")


if __name__ == "__main__":
    unittest.main()
