#!/usr/bin/env python
"""Persistent three-button Raspberry Pi image reader."""

import argparse
import os
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from read_aloud import DEFAULT_PLAYER, DEFAULT_VOICE
from reader_controller import ReaderController, ReaderError, ReaderState
from spoken_notices import SpokenNoticeCache, beep_pcm


def command_args(template, output_path):
    if "{output}" not in template:
        raise ReaderError("Command must contain the {output} placeholder.")
    return shlex.split(template.format(output=str(output_path)))


def capture_image(template, output_path, timeout=30):
    output_path = Path(output_path)
    try:
        subprocess.run(command_args(template, output_path), check=True, timeout=timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReaderError(f"Capture command failed: {exc}") from exc
    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise ReaderError("Capture command did not create a non-empty JPEG.")
    if output_path.suffix.lower() not in {".jpg", ".jpeg"}:
        raise ReaderError("Capture output must use a .jpg or .jpeg extension.")
    if output_path.read_bytes()[:2] != b"\xff\xd8":
        raise ReaderError("Capture command output is not a valid JPEG file.")
    return output_path


class Recorder:
    def __init__(self, template):
        self.template = template
        self.process = None
        self.output_path = None

    def start(self, output_path):
        if self.process is not None:
            raise ReaderError("Recorder is already running.")
        self.output_path = Path(output_path)
        if self.output_path.exists():
            self.output_path.unlink()
        try:
            self.process = subprocess.Popen(
                command_args(self.template, self.output_path)
            )
        except Exception as exc:
            self.output_path = None
            raise ReaderError(f"Record command failed to start: {exc}") from exc

    def stop(self, timeout=5, discard=False):
        process, output_path = self.process, self.output_path
        self.process = self.output_path = None
        if process is None:
            return None
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if discard:
            if output_path and output_path.exists():
                output_path.unlink()
            return None
        if not output_path or not output_path.is_file() or output_path.stat().st_size == 0:
            raise ReaderError("Record command did not create a non-empty WAV file.")
        header = output_path.read_bytes()[:12]
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise ReaderError("Record command output is not a valid WAV file.")
        return output_path


def play_pcm(player_command, chunks):
    player = subprocess.Popen(shlex.split(player_command), stdin=subprocess.PIPE)
    try:
        for chunk in chunks:
            player.stdin.write(chunk)
            player.stdin.flush()
        player.stdin.close()
        player.wait()
    except Exception:
        player.kill()
        player.wait()
        raise


class DeviceReader:
    def __init__(self, args, client, button_class=None):
        self.args = args
        self.notice_cache = SpokenNoticeCache(client)
        self.controller = ReaderController(
            client, ocr_model=args.ocr_model, tts_model=args.tts_model,
            summary_model=args.summary_model, stt_model=args.stt_model,
            qa_model=args.qa_model, voice=args.voice,
            notice_cache=self.notice_cache,
        )
        self.events = queue.Queue()
        self.recorder = Recorder(args.record_command)
        self.workdir = tempfile.TemporaryDirectory(prefix="img2speech-")
        self.player = None
        self.player_lock = threading.Lock()
        self.record_timer = None
        if button_class is None:
            from gpiozero import Button
            button_class = Button
        self.buttons = []
        for number, pin in enumerate(
                (args.button1_pin, args.button2_pin, args.button3_pin), 1):
            button = button_class(pin, pull_up=True, bounce_time=0.1)
            button.when_pressed = lambda n=number: self.events.put(("button", n))
            self.buttons.append(button)

    def stop_player(self):
        with self.player_lock:
            player = self.player
            self.player = None
        if player and player.poll() is None:
            player.kill()
            player.wait()

    def start_audio(self, task):
        if task is None:
            return
        self.stop_player()

        def worker():
            player = subprocess.Popen(shlex.split(self.args.player), stdin=subprocess.PIPE)
            with self.player_lock:
                self.player = player
            try:
                for chunk in self.controller.iter_audio(task):
                    player.stdin.write(chunk)
                    player.stdin.flush()
                player.stdin.close()
                player.wait()
            except (BrokenPipeError, OSError):
                if player.poll() is None:
                    player.kill()
                    player.wait()
            except Exception as exc:
                if player.poll() is None:
                    player.kill()
                    player.wait()
                self.events.put(("playback_error", exc))
            finally:
                with self.player_lock:
                    if self.player is player:
                        self.player = None

        threading.Thread(target=worker, daemon=True).start()

    def feedback(self, count=1, frequency=880):
        self.stop_player()
        play_pcm(self.args.player, [beep_pcm(count, frequency)])

    def speak_notice(self, event):
        self.stop_player()
        pcm = self.notice_cache.safe_event_pcm(
            self.args.tts_model, self.args.voice, event
        )
        play_pcm(self.args.player, [pcm])

    def report_error(self, event, error):
        print(f"{event}: {error}", file=sys.stderr)
        try:
            self.speak_notice(event)
        except Exception:
            try:
                self.feedback(3, 330)
            except Exception:
                pass

    @staticmethod
    def notice_for_exception(error):
        message = str(error).lower()
        if "no active image" in message:
            return "no_image"
        if "no spoken text" in message:
            return "no_content"
        if "capture" in message or "jpeg" in message:
            return "camera_error"
        if "record" in message or "wav" in message:
            return "microphone_error"
        return "generic_error"

    def async_call(self, label, function):
        def worker():
            try:
                result = function()
                self.events.put(("result", label, result, None))
            except Exception as exc:
                self.events.put(("result", label, None, exc))
        threading.Thread(target=worker, daemon=True).start()

    def new_image(self):
        self.stop_player()
        self.recorder.stop(discard=True)
        # Invalidate OCR before camera startup, which may take up to 30 seconds.
        self.controller.close()
        self.speak_notice("capture_start")
        image = Path(self.workdir.name) / f"capture-{time.time_ns()}.jpg"
        capture_image(self.args.capture_command, image)
        self.speak_notice("image_processing")
        self.start_audio(self.controller.load_image(image))

    def resume(self):
        self.stop_player()
        self.recorder.stop(discard=True)
        self.start_audio(self.controller.button1())

    def handle_button1(self):
        state = self.controller.state
        if state in {ReaderState.IDLE, ReaderState.READING, ReaderState.FINISHED}:
            self.new_image()
        else:
            try:
                self.resume()
            except ReaderError:
                self.new_image()

    def handle_button2(self):
        self.stop_player()
        self.recorder.stop(discard=True)
        self.controller.cancel_audio()
        self.speak_notice("summary_start")
        self.async_call("summary", self.controller.button2)

    def handle_button3(self):
        if self.controller.state == ReaderState.PROCESSING_QUESTION:
            self.speak_notice("question_wait")
            return
        if self.controller.state == ReaderState.RECORDING:
            if self.record_timer:
                self.record_timer.cancel()
                self.record_timer = None
            audio_path = self.recorder.stop()
            self.speak_notice("record_stop")
            self.async_call("answer", lambda: self.controller.button3_finish(audio_path))
            return
        self.stop_player()
        if not self.controller.button3_start():
            return
        self.speak_notice("record_start")
        audio_path = Path(self.workdir.name) / f"question-{time.time_ns()}.wav"
        self.recorder.start(audio_path)
        self.record_timer = threading.Timer(
            self.args.max_record_seconds,
            lambda: self.events.put(("button", 3)),
        )
        self.record_timer.daemon = True
        self.record_timer.start()

    def handle_result(self, label, result, error):
        if error:
            notice = self.notice_for_exception(error)
            if notice == "generic_error":
                notice = "model_error"
            self.report_error(notice, error)
            return
        self.start_audio(result)

    def run(self):
        print("Three-button reader ready. Press Ctrl+C to stop.")
        try:
            while True:
                event = self.events.get()
                try:
                    if event[0] == "button":
                        {1: self.handle_button1,
                         2: self.handle_button2,
                         3: self.handle_button3}[event[1]]()
                    elif event[0] == "result":
                        self.handle_result(event[1], event[2], event[3])
                    else:
                        self.report_error("playback_error", event[1])
                except Exception as exc:
                    self.report_error(self.notice_for_exception(exc), exc)
        except KeyboardInterrupt:
            pass
        finally:
            if self.record_timer:
                self.record_timer.cancel()
            self.recorder.stop(discard=True)
            self.stop_player()
            self.controller.close()
            for button in self.buttons:
                button.close()
            self.workdir.cleanup()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--button1-pin", type=int, required=True)
    parser.add_argument("--button2-pin", type=int, required=True)
    parser.add_argument("--button3-pin", type=int, required=True)
    parser.add_argument("--capture-command", required=True)
    parser.add_argument("--record-command", required=True)
    parser.add_argument("--player", default=DEFAULT_PLAYER)
    parser.add_argument(
        "--voice", default=DEFAULT_VOICE,
        help=f"TTS voice for all spoken output (default: {DEFAULT_VOICE})",
    )
    parser.add_argument("--ocr-model", default="gpt-4o-mini")
    parser.add_argument("--tts-model", default="gpt-4o-mini-tts")
    parser.add_argument("--summary-model", default="gpt-4o-mini")
    parser.add_argument("--stt-model", default="gpt-4o-mini-transcribe")
    parser.add_argument("--qa-model", default="gpt-4o-mini")
    parser.add_argument("--max-record-seconds", type=float, default=60)
    args = parser.parse_args()

    if len({args.button1_pin, args.button2_pin, args.button3_pin}) != 3:
        parser.error("The three GPIO pins must be different.")
    if args.max_record_seconds <= 0:
        parser.error("--max-record-seconds must be positive.")
    load_dotenv(Path(__file__).with_name(".env"))
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set.")
    DeviceReader(args, OpenAI()).run()


if __name__ == "__main__":
    main()
