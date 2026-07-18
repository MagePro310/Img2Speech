#!/usr/bin/env python
"""Read a photographed page aloud in near real time.

Streaming pipeline: OCR tokens stream in -> complete sentences are emitted as
segments (the first one as early as possible) -> each segment is synthesized
as raw PCM that streams straight into a long-lived audio player, so speech
starts a few seconds after the photo and plays continuously while the rest
of the page is still being processed.

Reads OPENAI_API_KEY from a .env file next to this script (or the environment).

Usage:
    uv run read_aloud.py photo.jpg [--voice onyx]
    uv run read_aloud.py photo.jpg --pcm-out out.pcm   # no audio device (testing)
"""

import argparse
import base64
import mimetypes
import os
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from ocr_to_speech import MAX_TTS_CHARS, OCR_PROMPT, TTS_INSTRUCTIONS

PCM_RATE = 24000            # gpt TTS "pcm" output: 24 kHz, 16-bit, mono
PCM_BYTES_PER_SEC = PCM_RATE * 2
DEFAULT_PLAYER = f"aplay -q -f S16_LE -r {PCM_RATE} -c 1 -t raw -"
SEGMENT_TARGET_CHARS = 300  # coalesce later sentences to roughly this size
SUMMARY_PROMPT = (
    "Tóm tắt nội dung sau thành tối đa 3 đến 5 ý chính ngắn gọn, rõ ràng bằng tiếng Việt. "
    "Nếu nội dung quá ngắn, hãy dùng ít ý hơn thay vì lặp lại hoặc suy đoán. "
    "Mỗi ý là một câu tự nhiên, phù hợp để đọc thành tiếng. "
    "Không thêm bất kỳ thông tin nào không có trong nội dung. "
    "Chỉ trả về bản tóm tắt, không thêm lời dẫn hay nhận xét."
)


@dataclass
class SpokenTracker:
    """Track exact PCM boundaries and select text heard by a given time."""

    first_text: str | None = None
    completed: list[tuple[str, float]] = field(default_factory=list)
    total_bytes: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def start_segment(self, text):
        with self._lock:
            if self.first_text is None:
                self.first_text = text

    def finish_segment(self, text, byte_count):
        with self._lock:
            self.total_bytes += byte_count
            self.completed.append((text, self.total_bytes / PCM_BYTES_PER_SEC))

    def text_for_seconds(self, played_seconds):
        """Return completed segments, falling back to the first started one."""
        with self._lock:
            selected = [text for text, end in self.completed if end <= played_seconds]
            if selected:
                return "\n\n".join(selected)
            return self.first_text or ""


def _cancelled(cancel_event):
    return cancel_event is not None and cancel_event.is_set()


def _put_chunk(chunk_queue, value, cancel_event):
    """Bounded queue put that does not strand a TTS worker after cancellation."""
    while not _cancelled(cancel_event):
        try:
            chunk_queue.put(value, timeout=0.1)
            return True
        except queue.Full:
            pass
    return False


def log(t0, msg):
    print(f"[{time.perf_counter() - t0:6.2f}s] {msg}", flush=True)


def stream_ocr(client, model, image_path):
    """Yield OCR text deltas as the vision model generates them."""
    mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    stream = client.chat.completions.create(
        model=model,
        stream=True,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
    )
    for event in stream:
        delta = event.choices[0].delta.content if event.choices else None
        if delta:
            yield delta


def take_segment(buffer, first, target=SEGMENT_TARGET_CHARS, limit=MAX_TTS_CHARS):
    """Return (segment, remainder) when a segment is ready, else (None, buffer).

    Cuts only at sentence/paragraph boundaries. The first segment is cut at the
    earliest boundary so audio can start ASAP; later ones coalesce to ~target.
    """
    cuts = [m.end() for m in re.finditer(r"[.!?…]\s+|\n\s*\n", buffer)]
    cut = None
    if cuts:
        if first:
            cut = cuts[0]
        else:
            ready = [c for c in cuts if c <= limit]
            if ready and ready[-1] >= target:
                cut = ready[-1]
    if cut is None and len(buffer) > limit:  # single huge sentence: hard split
        cut = limit
    if cut is None:
        return None, buffer
    return buffer[:cut].strip(), buffer[cut:]


def segment_worker(client, model, image_path, seg_queue, t0, transcript_queue=None,
                   cancel_event=None):
    """OCR-stream the image and push text segments onto seg_queue.

    When supplied, transcript_queue receives the complete OCR text (or the same
    exception sent to seg_queue) without changing the streaming pipeline.
    """
    try:
        buffer, total, first = "", 0, True
        transcript_parts = []
        for delta in stream_ocr(client, model, image_path):
            if _cancelled(cancel_event):
                break
            if total == 0:
                log(t0, "first OCR token")
            total += len(delta)
            transcript_parts.append(delta)
            buffer += delta
            while True:
                seg, buffer = take_segment(buffer, first)
                if seg is None:
                    break
                seg_queue.put(seg)
                first = False
        tail = buffer.strip()
        if tail and not _cancelled(cancel_event):
            seg_queue.put(tail)
        log(t0, f"OCR {'cancelled' if _cancelled(cancel_event) else 'done'} ({total} chars)")
        if transcript_queue is not None:
            transcript_queue.put("".join(transcript_parts).strip())
        seg_queue.put(None)
    except Exception as e:
        if transcript_queue is not None:
            transcript_queue.put(e)
        seg_queue.put(e)


def summarize_text(client, model, text):
    """Return a short Vietnamese summary suitable for spoken playback."""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SUMMARY_PROMPT},
            {"role": "user", "content": text},
        ],
    )
    return resp.choices[0].message.content.strip()


def stream_tts(client, model, voice, text):
    """Yield raw PCM chunks for `text` as they arrive from the TTS API."""
    # `instructions` is only accepted by the gpt-* TTS models, not tts-1/tts-1-hd
    kwargs = {"instructions": TTS_INSTRUCTIONS} if model.startswith("gpt-") else {}
    with client.audio.speech.with_streaming_response.create(
        model=model, voice=voice, input=text, response_format="pcm", **kwargs
    ) as resp:
        yield from resp.iter_bytes(chunk_size=4096)


def pump(client, model, voice, n, seg, chunk_queue, t0, cancel_event=None):
    """Stream one segment's TTS audio into its chunk queue."""
    try:
        total = 0
        for chunk in stream_tts(client, model, voice, seg):
            if _cancelled(cancel_event) or not _put_chunk(chunk_queue, chunk, cancel_event):
                return
            total += len(chunk)
        log(t0, f"segment {n} audio complete ({total / PCM_BYTES_PER_SEC:.1f}s of audio)")
        _put_chunk(chunk_queue, None, cancel_event)
    except Exception as e:
        if not _cancelled(cancel_event):
            _put_chunk(chunk_queue, e, cancel_event)


def tts_worker(client, model, voice, seg_queue, audio_queue, t0, cancel_event=None):
    """Open each segment's TTS stream as soon as its text arrives (up to 2
    concurrently), buffering chunks per segment; playback drains the buffers
    strictly in order. Starting segment N+1's stream while N is still playing
    is what keeps the speaker from going silent between segments.
    """
    pool = ThreadPoolExecutor(max_workers=2)
    n = 0
    try:
        while not _cancelled(cancel_event):
            try:
                seg = seg_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if seg is None or isinstance(seg, Exception):
                audio_queue.put(seg)
                return
            n += 1
            log(t0, f"segment {n} text ready ({len(seg)} chars)")
            chunk_queue = queue.Queue(maxsize=64)  # ~256 KB ≈ 5 s buffered per segment
            pool.submit(pump, client, model, voice, n, seg, chunk_queue, t0, cancel_event)
            audio_queue.put((seg, chunk_queue))
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def setup_summary_button(pin, on_press, armed_event, button_class=None):
    """Create an active-low GPIO button that triggers only after playback starts."""
    if button_class is None:
        try:
            from gpiozero import Button
        except (ImportError, OSError) as e:
            raise RuntimeError(
                "GPIO support is unavailable. Install gpiozero and run this on a Raspberry Pi."
            ) from e
        button_class = Button

    try:
        button = button_class(pin, pull_up=True, bounce_time=0.1)
    except Exception as e:
        raise RuntimeError(
            f"Cannot initialize BCM GPIO {pin}. Check the pin number and Raspberry Pi GPIO access."
        ) from e
    def handle_press():
        if armed_event.is_set():
            on_press()

    button.when_pressed = handle_press
    return button


def play_tts_text(client, model, voice, text, player_command, t0):
    """Stream one piece of text through TTS into a fresh PCM player."""
    try:
        player = subprocess.Popen(shlex.split(player_command), stdin=subprocess.PIPE)
    except FileNotFoundError:
        raise RuntimeError(f"Player not found: {player_command!r}") from None

    total = 0
    try:
        for chunk in stream_tts(client, model, voice, text):
            if total == 0:
                log(t0, "first summary audio byte -> player")
            player.stdin.write(chunk)
            player.stdin.flush()
            total += len(chunk)
        player.stdin.close()
        player.wait()
    except Exception:
        player.kill()
        raise
    return total


@dataclass
class PlaybackResult:
    total_bytes: int
    started_at: float | None
    interrupted: bool


def play_audio_queue(audio_queue, sink, player, stop_event, armed_event,
                     cancel_event, tracker, t0):
    """Drain ordered segment audio, stopping promptly when stop_event is set."""
    total_bytes = 0
    playback_started = None
    interrupted = False

    while not stop_event.is_set():
        try:
            item = audio_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        if item is None:
            break
        if isinstance(item, Exception):
            raise item
        seg, chunk_queue = item
        tracker.start_segment(seg)
        segment_bytes = 0
        segment_complete = False
        while not stop_event.is_set():
            try:
                chunk = chunk_queue.get(timeout=0.05)
            except queue.Empty:
                continue
            if chunk is None:
                segment_complete = True
                break
            if isinstance(chunk, Exception):
                raise chunk
            if total_bytes == 0:
                playback_started = time.perf_counter()
                armed_event.set()
                log(t0, "first audio byte -> player")
            sink.write(chunk)
            sink.flush()
            total_bytes += len(chunk)
            segment_bytes += len(chunk)
        if segment_complete:
            tracker.finish_segment(seg, segment_bytes)

    if stop_event.is_set():
        interrupted = True
        cancel_event.set()
        if player:
            player.kill()
        try:
            sink.close()
        except BrokenPipeError:
            pass
        if player:
            player.wait()
    else:
        sink.close()
        if player:
            while player.poll() is None:
                if stop_event.wait(0.05):
                    interrupted = True
                    cancel_event.set()
                    player.kill()
                    break
            player.wait()

    return PlaybackResult(total_bytes, playback_started, interrupted)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("image", type=Path, help="photo to read aloud")
    ap.add_argument("--voice", default="onyx", help="TTS voice (default: onyx)")
    ap.add_argument("--ocr-model", default="gpt-4o-mini", help="vision model for OCR")
    ap.add_argument("--tts-model", default="gpt-4o-mini-tts", help="TTS model")
    ap.add_argument("--summary-model", default="gpt-4o-mini",
                    help="model used to summarize the OCR text")
    ap.add_argument("--summary-button-pin", type=int, nargs="?", const=17, metavar="GPIO",
                    help="stop and summarize with a button on this BCM GPIO "
                         "(default pin when the flag has no value: 17)")
    ap.add_argument("--player", default=DEFAULT_PLAYER,
                    help="command that plays raw PCM from stdin (default: aplay)")
    ap.add_argument("--pcm-out", type=Path,
                    help="write raw PCM to this file instead of playing")
    args = ap.parse_args()

    if args.summary_button_pin is not None and args.pcm_out:
        ap.error("--summary-button-pin cannot be combined with --pcm-out")

    load_dotenv(Path(__file__).with_name(".env"))
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Put it in .env next to this script or export it.")
    if not args.image.is_file():
        sys.exit(f"Image not found: {args.image}")
    client = OpenAI()

    t0 = time.perf_counter()
    stop_event = threading.Event()
    armed_event = threading.Event()
    stop_times = []

    def request_summary():
        if not stop_event.is_set():
            stop_times.append(time.perf_counter())
            stop_event.set()

    button = None
    if args.summary_button_pin is not None:
        try:
            button = setup_summary_button(
                args.summary_button_pin, request_summary, armed_event
            )
        except Exception as e:
            sys.exit(f"FAILED to initialize summary button: {e}")

    if args.pcm_out:
        player, sink = None, open(args.pcm_out, "wb")
    else:
        try:
            player = subprocess.Popen(shlex.split(args.player), stdin=subprocess.PIPE)
        except FileNotFoundError:
            if button:
                button.close()
            sys.exit(f"Player not found: {args.player!r} — use --player or --pcm-out.")
        sink = player.stdin

    seg_queue = queue.Queue()
    audio_queue = queue.Queue()  # ordered per-segment chunk queues
    transcript_queue = queue.Queue() if args.summary_button_pin is not None else None
    cancel_event = threading.Event()
    tracker = SpokenTracker()
    threading.Thread(target=segment_worker, daemon=True,
                     args=(client, args.ocr_model, args.image, seg_queue, t0,
                           transcript_queue, cancel_event)).start()
    threading.Thread(target=tts_worker, daemon=True,
                     args=(client, args.tts_model, args.voice, seg_queue, audio_queue, t0,
                           cancel_event)).start()
    log(t0, f"OCR started ({args.ocr_model}; TTS {args.tts_model}, voice={args.voice})")

    try:
        playback = play_audio_queue(
            audio_queue, sink, player, stop_event, armed_event,
            cancel_event, tracker, t0
        )
    except BrokenPipeError:
        cancel_event.set()
        sys.exit("Audio player exited unexpectedly — check the audio device, "
                 "or use --player / --pcm-out.")
    except Exception as e:
        cancel_event.set()
        if player:
            player.kill()
        sys.exit(f"FAILED: {e}")

    if playback.total_bytes == 0:
        cancel_event.set()
        if button:
            button.close()
        sys.exit("No text found in image.")

    if args.summary_button_pin is not None:
        try:
            if playback.interrupted:
                played_seconds = max(0.0, stop_times[0] - playback.started_at)
                summary_input = tracker.text_for_seconds(played_seconds)
                log(t0, f"reading stopped after {played_seconds:.1f}s of audio")
            else:
                log(t0, f"done: {playback.total_bytes / PCM_BYTES_PER_SEC:.1f}s of audio, "
                        "playback finished")
                log(t0, f"waiting for summary button on BCM GPIO {args.summary_button_pin}")
                stop_event.wait()
                transcript = transcript_queue.get()
                if isinstance(transcript, Exception):
                    raise transcript
                summary_input = transcript
            cancel_event.set()
            button.close()
            if not summary_input:
                raise RuntimeError("No spoken text is available to summarize.")
            log(t0, f"summary requested ({args.summary_model})")
            summary = summarize_text(client, args.summary_model, summary_input)
            if not summary:
                raise RuntimeError("The summary model returned no text.")
            summary_bytes = play_tts_text(
                client, args.tts_model, args.voice, summary, args.player, t0
            )
            log(t0, f"summary done: {summary_bytes / PCM_BYTES_PER_SEC:.1f}s of audio")
        except Exception as e:
            if button:
                button.close()
            sys.exit(f"FAILED to read summary: {e}")
    else:
        log(t0, f"done: {playback.total_bytes / PCM_BYTES_PER_SEC:.1f}s of audio, "
                "playback finished")


if __name__ == "__main__":
    main()
