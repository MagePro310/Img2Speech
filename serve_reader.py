#!/usr/bin/env python
"""Browser simulator for the persistent three-button image reader."""

import argparse
import json
import os
import re
import struct
import sys
import tempfile
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
from openai import OpenAI

from read_aloud import PCM_RATE
from reader_controller import ReaderController, ReaderError

ROOT = Path(__file__).parent
MAX_SESSIONS = 32
MAX_UPLOAD = 20 * 1024 * 1024
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
AUDIO_TYPES = {
    "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/webm": ".webm", "audio/ogg": ".ogg",
}
IMAGE_TYPES = {
    "image/jpeg": (".jpg", b"\xff\xd8"),
    "image/png": (".png", b"\x89PNG\r\n\x1a\n"),
}


@dataclass
class WebSession:
    controller: ReaderController
    tempdir: tempfile.TemporaryDirectory
    tasks: dict = field(default_factory=dict)

    def close(self):
        self.controller.close()
        self.tempdir.cleanup()


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>Img2Speech — three-button reader</title>
<style>
body { font-family: sans-serif; max-width: 46rem; margin: 2rem auto; padding: 0 1rem; }
.buttons { display: grid; gap: .7rem; grid-template-columns: repeat(3, 1fr); }
button { min-height: 4rem; font-size: 1rem; padding: .6rem; }
#status { margin: 1rem 0; font-weight: bold; }
.qa { background: #f4f4f4; padding: 1rem; min-height: 4rem; white-space: pre-wrap; }
audio { width: 100%; margin: 1rem 0; }
</style>
<h1>Img2Speech</h1>
<div class="buttons">
  <button id="b1">1 — Chụp/nạp ảnh · Đọc tiếp</button>
  <button id="b2">2 — Dừng và tóm tắt</button>
  <button id="b3">3 — Bắt đầu hỏi</button>
</div>
<input id="image" type="file" accept="image/jpeg,image/png,.jpg,.jpeg,.png" capture="environment" hidden>
<audio id="audio" controls></audio>
<div id="status">Chưa có ảnh</div>
<div class="qa"><b>Câu hỏi:</b> <span id="question"></span><br><br>
<b>Trả lời:</b> <span id="answer"></span></div>
<script>
const sid = (crypto.randomUUID ? crypto.randomUUID()
  : Date.now().toString(36) + Math.random().toString(36).slice(2));
const audio = document.getElementById('audio');
const imageInput = document.getElementById('image');
const statusEl = document.getElementById('status');
const b3 = document.getElementById('b3');
let state = 'idle';
let recorder = null;
let mediaStream = null;
let recordedChunks = [];

function stopAudio() { audio.pause(); audio.removeAttribute('src'); audio.load(); }
function show(data) {
  state = data.state;
  statusEl.textContent = 'Trạng thái: ' + state;
  document.getElementById('question').textContent = data.question || '';
  document.getElementById('answer').textContent = data.answer || '';
  b3.textContent = state === 'recording' ? '3 — Dừng và gửi câu hỏi' : '3 — Bắt đầu hỏi';
  if (data.audio_url) {
    audio.src = data.audio_url;
    audio.play().catch(err => statusEl.textContent = 'Không phát được audio: ' + err);
  }
}
async function request(path, options={}) {
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  show(data);
  return data;
}
function fail(error) { statusEl.textContent = 'Lỗi: ' + error.message; }

document.getElementById('b1').onclick = async () => {
  try {
    if (recorder) discardRecording();
    if (['idle', 'reading', 'finished'].includes(state)) imageInput.click();
    else {
      stopAudio();
      await request('/button/1?sid=' + encodeURIComponent(sid), {method:'POST'});
    }
  } catch (error) { fail(error); }
};
imageInput.onchange = async () => {
  if (!imageInput.files.length) return;
  try {
    stopAudio();
    const file = imageInput.files[0];
    const mime = file.type || (file.name.toLowerCase().endsWith('.png')
      ? 'image/png' : 'image/jpeg');
    await request('/button/1?sid=' + encodeURIComponent(sid), {
      method:'POST', headers:{'Content-Type':mime}, body:file
    });
    imageInput.value = '';
  } catch (error) { fail(error); }
};
document.getElementById('b2').onclick = async () => {
  try {
    if (recorder) discardRecording();
    stopAudio();
    statusEl.textContent = 'Đang tạo bản tóm tắt…';
    await request('/button/2?sid=' + encodeURIComponent(sid), {method:'POST'});
  } catch (error) { fail(error); }
};

function discardRecording() {
  if (!recorder) return;
  recorder.onstop = null;
  recorder.stop();
  mediaStream.getTracks().forEach(track => track.stop());
  recorder = null; mediaStream = null; recordedChunks = [];
}
async function beginRecording() {
  stopAudio();
  mediaStream = await navigator.mediaDevices.getUserMedia({audio:true});
  try {
    await request('/button/3?sid=' + encodeURIComponent(sid), {method:'POST'});
  } catch (error) {
    mediaStream.getTracks().forEach(track => track.stop());
    mediaStream = null;
    throw error;
  }
  recordedChunks = [];
  const preferred = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : 'audio/ogg';
  recorder = new MediaRecorder(mediaStream, {mimeType: preferred});
  recorder.ondataavailable = event => { if (event.data.size) recordedChunks.push(event.data); };
  recorder.onstop = async () => {
    const mime = recorder.mimeType.split(';')[0];
    const blob = new Blob(recordedChunks, {type:mime});
    mediaStream.getTracks().forEach(track => track.stop());
    recorder = null; mediaStream = null; recordedChunks = [];
    try {
      statusEl.textContent = 'Đang nhận dạng và trả lời câu hỏi…';
      await request('/button/3?sid=' + encodeURIComponent(sid), {
        method:'POST', headers:{'Content-Type':mime}, body:blob
      });
    } catch (error) { fail(error); }
  };
  recorder.start();
  show({state:'recording'});
}
document.getElementById('b3').onclick = async () => {
  try {
    if (recorder) recorder.stop(); else await beginRecording();
  } catch (error) { fail(error); }
};
audio.addEventListener('ended', async () => {
  try { show(await (await fetch('/status?sid=' + encodeURIComponent(sid))).json()); }
  catch (error) { fail(error); }
});
</script>
"""


def wav_header(rate=PCM_RATE):
    return (b"RIFF\xff\xff\xff\xffWAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data\xff\xff\xff\xff")


def image_suffix(content_type, body):
    image_type = IMAGE_TYPES.get(content_type)
    if image_type is None:
        raise ReaderError("button 1 accepts PNG or JPEG images only")
    suffix, signature = image_type
    if not body.startswith(signature):
        raise ReaderError(f"uploaded data is not a valid {content_type} image")
    return suffix


class ReaderHandler(BaseHTTPRequestHandler):
    cfg = None
    oai_client = None
    sessions = OrderedDict()
    session_lock = threading.Lock()

    def log_message(self, fmt, *args):
        pass

    def send_json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def error_json(self, status, message):
        self.send_json(status, {"error": message})

    def valid_sid(self, sid):
        if not SESSION_RE.fullmatch(sid):
            self.error_json(400, "invalid session id")
            return False
        return True

    @classmethod
    def get_session(cls, sid):
        with cls.session_lock:
            session = cls.sessions.get(sid)
            if session:
                cls.sessions.move_to_end(sid)
            return session

    @classmethod
    def create_session(cls, sid):
        cfg = cls.cfg
        session = WebSession(
            ReaderController(
                cls.oai_client, ocr_model=cfg.ocr_model, tts_model=cfg.tts_model,
                summary_model=cfg.summary_model, stt_model=cfg.stt_model,
                qa_model=cfg.qa_model, voice=cfg.voice,
            ),
            tempfile.TemporaryDirectory(prefix="img2speech-web-"),
        )
        with cls.session_lock:
            old = cls.sessions.pop(sid, None)
            if old:
                old.close()
            cls.sessions[sid] = session
            while len(cls.sessions) > MAX_SESSIONS:
                _, expired = cls.sessions.popitem(last=False)
                expired.close()
        return session

    def read_body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ReaderError("invalid Content-Length")
        if length < 0 or length > MAX_UPLOAD:
            raise ReaderError("upload exceeds the 20 MiB limit")
        return self.rfile.read(length) if length else b""

    @staticmethod
    def response_for(session, task=None):
        current_generation = session.controller.generation
        session.tasks = {
            generation: existing for generation, existing in session.tasks.items()
            if generation == current_generation and not existing.cancel_event.is_set()
        }
        data = session.controller.status()
        if task:
            session.tasks[task.generation] = task
            data["generation"] = task.generation
            data["audio_url"] = f"/audio?sid=__SID__&generation={task.generation}"
        return data

    def finish_response(self, sid, session, task=None):
        data = self.response_for(session, task)
        if "audio_url" in data:
            data["audio_url"] = data["audio_url"].replace("__SID__", sid)
        self.send_json(200, data)

    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)
        if url.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        sid = params.get("sid", [""])[0]
        if not self.valid_sid(sid):
            return
        session = self.get_session(sid)
        if not session:
            self.error_json(409, "unknown or expired session")
            return
        if url.path == "/status":
            self.send_json(200, session.controller.status())
        elif url.path == "/audio":
            self.stream_audio(session, params.get("generation", [""])[0])
        else:
            self.error_json(404, "not found")

    def do_POST(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)
        sid = params.get("sid", [""])[0]
        if not self.valid_sid(sid):
            return
        try:
            body = self.read_body()
            if url.path == "/button/1":
                self.button1(sid, body)
            elif url.path == "/button/2":
                self.button2(sid, body)
            elif url.path == "/button/3":
                self.button3(sid, body)
            else:
                self.error_json(404, "not found")
        except ReaderError as exc:
            self.error_json(409, str(exc))
        except Exception as exc:
            self.error_json(502, str(exc))

    def button1(self, sid, body):
        session = self.get_session(sid)
        if body:
            suffix = image_suffix(self.headers.get_content_type(), body)
            # A new image owns a fresh temp directory and removes every file,
            # OCR worker, cursor, and displayed Q&A value from the old image.
            session = self.create_session(sid)
            image_path = Path(session.tempdir.name) / (
                f"capture-{session.controller.generation + 1}{suffix}"
            )
            image_path.write_bytes(body)
            task = session.controller.load_image(image_path)
        else:
            if session is None:
                raise ReaderError("button 1 requires an image")
            task = session.controller.button1()
        self.finish_response(sid, session, task)

    def button2(self, sid, body):
        if body:
            raise ReaderError("button 2 does not accept a request body")
        session = self.get_session(sid)
        if not session:
            raise ReaderError("no active image")
        self.finish_response(sid, session, session.controller.button2())

    def button3(self, sid, body):
        session = self.get_session(sid)
        if not session:
            raise ReaderError("no active image")
        if not body:
            session.controller.button3_start()
            self.finish_response(sid, session)
            return
        content_type = self.headers.get_content_type()
        suffix = AUDIO_TYPES.get(content_type)
        if suffix is None:
            raise ReaderError("button 3 accepts WAV, WebM, or Ogg audio only")
        audio_path = Path(session.tempdir.name) / f"question-{session.controller.generation + 1}{suffix}"
        audio_path.write_bytes(body)
        try:
            task = session.controller.button3_finish(audio_path)
        finally:
            audio_path.unlink(missing_ok=True)
        self.finish_response(sid, session, task)

    def stream_audio(self, session, generation_value):
        try:
            generation = int(generation_value)
        except ValueError:
            self.error_json(400, "invalid audio generation")
            return
        task = session.tasks.pop(generation, None)
        if task is None:
            self.error_json(409, "audio generation is unknown or already consumed")
            return
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(wav_header())
            for chunk in session.controller.iter_audio(task):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            task.cancel_event.set()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--voice", default="onyx")
    parser.add_argument("--ocr-model", default="gpt-4o-mini")
    parser.add_argument("--tts-model", default="gpt-4o-mini-tts")
    parser.add_argument("--summary-model", default="gpt-4o-mini")
    parser.add_argument("--stt-model", default="gpt-4o-mini-transcribe")
    parser.add_argument("--qa-model", default="gpt-4o-mini")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set.")
    ReaderHandler.cfg = args
    ReaderHandler.oai_client = OpenAI()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ReaderHandler)
    print(f"Serving on http://localhost:{args.port}. Ctrl+C to stop.")
    try:
        server.serve_forever()
    finally:
        with ReaderHandler.session_lock:
            for session in ReaderHandler.sessions.values():
                session.close()
            ReaderHandler.sessions.clear()


if __name__ == "__main__":
    main()
