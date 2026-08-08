import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import device_reader
import ocr_to_speech
import read_aloud
import reader_controller


class SilentNoticeCache:
    def safe_event_pcm(self, model, voice, event):
        return b""


class OcrModelRequestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.image_path = Path(self.tempdir.name, "page.jpg")
        self.image_path.write_bytes(b"fake JPEG bytes")

    def tearDown(self):
        self.tempdir.cleanup()

    def assert_ocr_content(self, request):
        content = request["messages"][0]["content"]
        self.assertEqual(content[0], {
            "type": "text",
            "text": ocr_to_speech.OCR_PROMPT,
        })
        self.assertEqual(content[1]["type"], "image_url")
        self.assertEqual(content[1]["image_url"]["detail"], "high")
        self.assertTrue(
            content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        )

    def test_batch_default_ocr_uses_high_detail_and_max_reasoning(self):
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="  Văn bản nhận dạng.  ")
            )]
        )

        result = ocr_to_speech.ocr_image(
            client, ocr_to_speech.DEFAULT_OCR_MODEL, self.image_path
        )

        self.assertEqual(result, "Văn bản nhận dạng.")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-sol")
        self.assertEqual(request["reasoning_effort"], "max")
        self.assertNotIn("stream", request)
        self.assert_ocr_content(request)

    def test_batch_non_default_ocr_omits_reasoning_effort(self):
        client = MagicMock()
        client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Văn bản"))]
        )

        ocr_to_speech.ocr_image(client, "custom-vision-model", self.image_path)

        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "custom-vision-model")
        self.assertNotIn("reasoning_effort", request)
        self.assert_ocr_content(request)

    def test_streaming_default_ocr_streams_content_with_quality_options(self):
        client = MagicMock()
        client.chat.completions.create.return_value = iter([
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="Câu một. ")
            )]),
            SimpleNamespace(choices=[]),
            SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="Câu hai.")
            )]),
        ])

        deltas = list(read_aloud.stream_ocr(
            client, ocr_to_speech.DEFAULT_OCR_MODEL, self.image_path
        ))

        self.assertEqual(deltas, ["Câu một. ", "Câu hai."])
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "gpt-5.6-sol")
        self.assertIs(request["stream"], True)
        self.assertEqual(request["reasoning_effort"], "max")
        self.assert_ocr_content(request)

    def test_streaming_non_default_ocr_omits_reasoning_effort(self):
        client = MagicMock()
        client.chat.completions.create.return_value = iter([])

        self.assertEqual(
            list(read_aloud.stream_ocr(
                client, "custom-vision-model", self.image_path
            )),
            [],
        )

        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], "custom-vision-model")
        self.assertIs(request["stream"], True)
        self.assertNotIn("reasoning_effort", request)
        self.assert_ocr_content(request)


class ModelDefaultTests(unittest.TestCase):
    def test_reader_controller_uses_shared_model_and_voice_defaults(self):
        controller = reader_controller.ReaderController(
            MagicMock(), notice_cache=SilentNoticeCache()
        )
        try:
            self.assertEqual(controller.ocr_model, "gpt-5.6-sol")
            self.assertEqual(controller.tts_model, "gpt-4o-mini-tts")
            self.assertEqual(controller.voice, "marin")
        finally:
            controller.close()

    def test_device_parser_defaults_and_explicit_overrides(self):
        required = [
            "--capture-command", "capture --output {output}",
            "--record-command", "record {output}",
        ]
        defaults = device_reader.build_parser().parse_args(required)
        self.assertEqual(defaults.ocr_model, "gpt-5.6-sol")
        self.assertEqual(defaults.tts_model, "gpt-4o-mini-tts")
        self.assertEqual(defaults.voice, "marin")

        overridden = device_reader.build_parser().parse_args(required + [
            "--ocr-model", "custom-ocr",
            "--tts-model", "custom-tts",
            "--voice", "custom-voice",
        ])
        self.assertEqual(overridden.ocr_model, "custom-ocr")
        self.assertEqual(overridden.tts_model, "custom-tts")
        self.assertEqual(overridden.voice, "custom-voice")


if __name__ == "__main__":
    unittest.main()
