#!/usr/bin/env python
"""Browser test page for the live reader: streams the spoken page as WAV.

Serves a minimal page listing the .jpg images in this directory; clicking one
streams OCR->TTS audio into the browser's <audio> player while the page is
still being processed. Intended for use over VS Code port forwarding:
open http://localhost:8765 in the laptop browser.

Reads OPENAI_API_KEY from a .env file next to this script (or the environment).

Usage:
    uv run serve_reader.py [--port 8765] [--voice onyx]
"""

import argparse
import os
import queue
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from openai import OpenAI

from read_aloud import PCM_BYTES_PER_SEC, PCM_RATE, log, segment_worker, tts_worker

ROOT = Path(__file__).parent

PAGE = """<!doctype html>
<meta charset="utf-8">
<title>OCR reader — live test</title>
<style>
  body { font-family: sans-serif; max-width: 40em; margin: 2em auto; }
  button { display: block; margin: .4em 0; padding: .5em 1em; font-size: 1em; }
</style>
<h1>OCR reader — live test</h1>
<p>Click an image; speech starts a few seconds later, while the rest of the
page is still being processed.</p>
__BUTTONS__
<p><audio id="a" controls></audio></p>
<p id="s"></p>
<script>
function play(n) {
  document.getElementById('s').textContent = 'reading ' + n + ' ...';
  const a = document.getElementById('a');
  a.src = '/read?img=' + encodeURIComponent(n) + '&t=' + Date.now();
  a.play();
}
</script>
"""


def wav_header(rate=PCM_RATE):
    """RIFF header for a 16-bit mono PCM stream of unknown length."""
    return (b"RIFF\xff\xff\xff\xffWAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data\xff\xff\xff\xff")


class ReaderHandler(BaseHTTPRequestHandler):
    cfg = None         # argparse namespace, set in main()
    oai_client = None

    def log_message(self, fmt, *args):
        pass  # keep the console to pipeline milestones only

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            self.send_page()
        elif url.path == "/read":
            self.stream_reading(parse_qs(url.query).get("img", [""])[0])
        else:
            self.send_error(404)

    def send_page(self):
        images = sorted(p.name for p in ROOT.glob("*.jpg")) + sorted(
            p.name for p in ROOT.glob("*.jpeg"))
        buttons = "\n".join(
            f'<button onclick="play({name!r})">{name}</button>' for name in images
        ) or "<p><b>No .jpg images found next to serve_reader.py.</b></p>"
        body = PAGE.replace("__BUTTONS__", buttons).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream_reading(self, name):
        image = ROOT / name
        if Path(name).name != name or not image.is_file():
            self.send_error(404, "unknown image")
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        cfg = self.cfg
        t0 = time.perf_counter()
        log(t0, f"reading {name} for {self.client_address[0]} "
                f"({cfg.ocr_model}; {cfg.tts_model}, voice={cfg.voice})")
        seg_queue = queue.Queue()
        audio_queue = queue.Queue()
        threading.Thread(target=segment_worker, daemon=True,
                         args=(self.oai_client, cfg.ocr_model, image, seg_queue, t0)).start()
        threading.Thread(target=tts_worker, daemon=True,
                         args=(self.oai_client, cfg.tts_model, cfg.voice,
                               seg_queue, audio_queue, t0)).start()
        total = 0
        try:
            self.wfile.write(wav_header())
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
                    if total == 0:
                        log(t0, "first audio byte -> browser")
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    total += len(chunk)
            log(t0, f"done: {total / PCM_BYTES_PER_SEC:.1f}s of audio streamed")
        except (BrokenPipeError, ConnectionResetError):
            log(t0, "client disconnected")
        except Exception as e:
            log(t0, f"FAILED: {e}")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--voice", default="onyx", help="TTS voice (default: onyx)")
    ap.add_argument("--ocr-model", default="gpt-4o-mini", help="vision model for OCR")
    ap.add_argument("--tts-model", default="gpt-4o-mini-tts", help="TTS model")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Put it in .env next to this script or export it.")
    ReaderHandler.cfg = args
    ReaderHandler.oai_client = OpenAI()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReaderHandler)
    print(f"Serving on http://localhost:{args.port} — VS Code should forward the port. Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
