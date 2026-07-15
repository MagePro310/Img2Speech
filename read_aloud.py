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
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from ocr_to_speech import MAX_TTS_CHARS, OCR_PROMPT, TTS_INSTRUCTIONS

PCM_RATE = 24000            # gpt TTS "pcm" output: 24 kHz, 16-bit, mono
PCM_BYTES_PER_SEC = PCM_RATE * 2
DEFAULT_PLAYER = f"aplay -q -f S16_LE -r {PCM_RATE} -c 1 -t raw -"
SEGMENT_TARGET_CHARS = 300  # coalesce later sentences to roughly this size


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


def segment_worker(client, model, image_path, seg_queue, t0):
    """OCR-stream the image and push text segments onto seg_queue."""
    try:
        buffer, total, first = "", 0, True
        for delta in stream_ocr(client, model, image_path):
            if total == 0:
                log(t0, "first OCR token")
            total += len(delta)
            buffer += delta
            while True:
                seg, buffer = take_segment(buffer, first)
                if seg is None:
                    break
                seg_queue.put(seg)
                first = False
        tail = buffer.strip()
        if tail:
            seg_queue.put(tail)
        log(t0, f"OCR done ({total} chars)")
        seg_queue.put(None)
    except Exception as e:
        seg_queue.put(e)


def stream_tts(client, model, voice, text):
    """Yield raw PCM chunks for `text` as they arrive from the TTS API."""
    # `instructions` is only accepted by the gpt-* TTS models, not tts-1/tts-1-hd
    kwargs = {"instructions": TTS_INSTRUCTIONS} if model.startswith("gpt-") else {}
    with client.audio.speech.with_streaming_response.create(
        model=model, voice=voice, input=text, response_format="pcm", **kwargs
    ) as resp:
        yield from resp.iter_bytes(chunk_size=4096)


def pump(client, model, voice, n, seg, chunk_queue, t0):
    """Stream one segment's TTS audio into its chunk queue."""
    try:
        total = 0
        for chunk in stream_tts(client, model, voice, seg):
            chunk_queue.put(chunk)
            total += len(chunk)
        log(t0, f"segment {n} audio complete ({total / PCM_BYTES_PER_SEC:.1f}s of audio)")
        chunk_queue.put(None)
    except Exception as e:
        chunk_queue.put(e)


def tts_worker(client, model, voice, seg_queue, audio_queue, t0):
    """Open each segment's TTS stream as soon as its text arrives (up to 2
    concurrently), buffering chunks per segment; playback drains the buffers
    strictly in order. Starting segment N+1's stream while N is still playing
    is what keeps the speaker from going silent between segments.
    """
    pool = ThreadPoolExecutor(max_workers=2)
    n = 0
    while True:
        seg = seg_queue.get()
        if seg is None or isinstance(seg, Exception):
            audio_queue.put(seg)
            return
        n += 1
        log(t0, f"segment {n} text ready ({len(seg)} chars)")
        chunk_queue = queue.Queue(maxsize=64)  # ~256 KB ≈ 5 s of audio buffered per segment
        pool.submit(pump, client, model, voice, n, seg, chunk_queue, t0)
        audio_queue.put(chunk_queue)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("image", type=Path, help="photo to read aloud")
    ap.add_argument("--voice", default="onyx", help="TTS voice (default: onyx)")
    ap.add_argument("--ocr-model", default="gpt-4o-mini", help="vision model for OCR")
    ap.add_argument("--tts-model", default="gpt-4o-mini-tts", help="TTS model")
    ap.add_argument("--player", default=DEFAULT_PLAYER,
                    help="command that plays raw PCM from stdin (default: aplay)")
    ap.add_argument("--pcm-out", type=Path,
                    help="write raw PCM to this file instead of playing")
    args = ap.parse_args()

    load_dotenv(Path(__file__).with_name(".env"))
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Put it in .env next to this script or export it.")
    if not args.image.is_file():
        sys.exit(f"Image not found: {args.image}")
    client = OpenAI()

    if args.pcm_out:
        player, sink = None, open(args.pcm_out, "wb")
    else:
        try:
            player = subprocess.Popen(shlex.split(args.player), stdin=subprocess.PIPE)
        except FileNotFoundError:
            sys.exit(f"Player not found: {args.player!r} — use --player or --pcm-out.")
        sink = player.stdin

    t0 = time.perf_counter()
    seg_queue = queue.Queue()
    audio_queue = queue.Queue()  # ordered per-segment chunk queues
    threading.Thread(target=segment_worker, daemon=True,
                     args=(client, args.ocr_model, args.image, seg_queue, t0)).start()
    threading.Thread(target=tts_worker, daemon=True,
                     args=(client, args.tts_model, args.voice, seg_queue, audio_queue, t0)).start()
    log(t0, f"OCR started ({args.ocr_model}; TTS {args.tts_model}, voice={args.voice})")

    total_bytes = 0
    try:
        while True:
            item = audio_queue.get()
            if item is None:
                break
            if isinstance(item, Exception):
                raise item
            while True:
                chunk = item.get()
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    raise chunk
                if total_bytes == 0:
                    log(t0, "first audio byte -> player")
                sink.write(chunk)
                sink.flush()
                total_bytes += len(chunk)
        sink.close()
        if player:
            player.wait()  # aplay exits once its buffer has drained
    except BrokenPipeError:
        sys.exit("Audio player exited unexpectedly — check the audio device, "
                 "or use --player / --pcm-out.")
    except Exception as e:
        if player:
            player.kill()
        sys.exit(f"FAILED: {e}")

    if total_bytes == 0:
        sys.exit("No text found in image.")
    log(t0, f"done: {total_bytes / PCM_BYTES_PER_SEC:.1f}s of audio, playback finished")


if __name__ == "__main__":
    main()
