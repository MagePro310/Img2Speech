#!/usr/bin/env python
"""Persistent three-button Raspberry Pi image reader."""

import argparse
import math
import os
import queue
import shlex
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from read_aloud import DEFAULT_PLAYER, PCM_RATE
from reader_controller import ReaderController, ReaderError, ReaderState


def command_args(template, output_path):
    if "{output}" not in template:
        raise ReaderError("Command must contain the {output} placeholder.")
    return shlex.split(template.format(output=str(output_path)))


def capture_image(template, output_path, timeout=30):
    output_path = Path(output_path)
    subprocess.run(command_args(template, output_path), check=True, timeout=timeout)
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
        self.process = subprocess.Popen(command_args(self.template, self.output_path))

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


def beep_pcm(count=1, frequency=880, duration=0.09):
    samples = []
    tone_samples = int(PCM_RATE * duration)
    gap = b"\0\0" * int(PCM_RATE * 0.06)
    for index in range(count):
        for n in range(tone_samples):
            value = int(7000 * math.sin(2 * math.pi * frequency * n / PCM_RATE))
            samples.append(struct.pack("<h", value))
        if index + 1 < count:
            samples.append(gap)
    return b"".join(samples)


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
        self.controller = ReaderController(
            client, ocr_model=args.ocr_model, tts_model=args.tts_model,
            summary_model=args.summary_model, stt_model=args.stt_model,
            qa_model=args.qa_model, voice=args.voice,
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
        image = Path(self.workdir.name) / f"capture-{time.time_ns()}.jpg"
        capture_image(self.args.capture_command, image)
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
        self.async_call("summary", self.controller.button2)

    def handle_button3(self):
        if self.controller.state == ReaderState.PROCESSING_QUESTION:
            return
        if self.controller.state == ReaderState.RECORDING:
            if self.record_timer:
                self.record_timer.cancel()
                self.record_timer = None
            audio_path = self.recorder.stop()
            self.feedback(2)
            self.async_call("answer", lambda: self.controller.button3_finish(audio_path))
            return
        self.stop_player()
        if not self.controller.button3_start():
            return
        self.feedback(1)
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
            print(f"{label} failed: {error}", file=sys.stderr)
            self.feedback(3, 330)
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
                        print(f"playback failed: {event[1]}", file=sys.stderr)
                        self.feedback(3, 330)
                except Exception as exc:
                    print(f"button action failed: {exc}", file=sys.stderr)
                    self.feedback(3, 330)
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
    parser.add_argument("--voice", default="onyx")
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
