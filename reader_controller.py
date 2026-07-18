"""Shared resumable reader state machine for GPIO and browser adapters."""

import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from read_aloud import stream_ocr, stream_tts, summarize_text


class ReaderError(RuntimeError):
    pass


class ReaderState(str, Enum):
    IDLE = "idle"
    READING = "reading"
    SUMMARIZING = "summarizing"
    PAUSED = "paused"
    RECORDING = "recording"
    PROCESSING_QUESTION = "processing_question"
    ANSWERING = "answering"
    FINISHED = "finished"
    ERROR = "error"


@dataclass
class AudioTask:
    generation: int
    kind: str
    cancel_event: threading.Event = field(default_factory=threading.Event)
    text: str | None = None


@dataclass
class DocumentSession:
    image_path: Path
    sentences: list[str] = field(default_factory=list)
    cursor: int = 0
    started_index: int | None = None
    ocr_done: bool = False
    ocr_error: Exception | None = None
    ocr_cancel: threading.Event = field(default_factory=threading.Event)
    latest_question: str | None = None
    latest_answer: str | None = None
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def append_sentence(self, text):
        text = text.strip()
        if not text:
            return
        with self.condition:
            self.sentences.append(text)
            self.condition.notify_all()

    def mark_ocr_done(self, error=None):
        with self.condition:
            self.ocr_error = error
            self.ocr_done = True
            self.condition.notify_all()

    def has_unread(self):
        with self.condition:
            return self.cursor < len(self.sentences) or not self.ocr_done

    def source_text(self):
        """Original text heard completely; first started sentence is the fallback."""
        with self.condition:
            if self.cursor:
                return "\n\n".join(self.sentences[:self.cursor])
            if self.started_index is not None and self.started_index < len(self.sentences):
                return self.sentences[self.started_index]
            return ""


QA_PROMPT = (
    "Bạn là trợ lý đọc sách tiếng Việt. Chỉ trả lời câu hỏi bằng thông tin có trong "
    "NỘI DUNG ĐÃ ĐỌC được cung cấp. Không dùng kiến thức ngoài, không suy đoán. "
    "Nếu nội dung không đủ để trả lời, hãy nói rõ rằng phần đã đọc chưa có thông tin đó. "
    "Trả lời ngắn gọn, tự nhiên và phù hợp để đọc thành tiếng."
)


def transcribe_question(client, model, audio_path):
    with open(audio_path, "rb") as audio_file:
        response = client.audio.transcriptions.create(
            model=model, file=audio_file, language="vi"
        )
    text = getattr(response, "text", response)
    return str(text).strip()


def answer_question(client, model, source_text, question):
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": QA_PROMPT},
            {"role": "user", "content": (
                f"NỘI DUNG ĐÃ ĐỌC:\n{source_text}\n\nCÂU HỎI:\n{question}"
            )},
        ],
    )
    return response.choices[0].message.content.strip()


class ReaderController:
    def __init__(self, client, *, ocr_model="gpt-4o-mini",
                 tts_model="gpt-4o-mini-tts", summary_model="gpt-4o-mini",
                 stt_model="gpt-4o-mini-transcribe", qa_model="gpt-4o-mini",
                 voice="onyx"):
        self.client = client
        self.ocr_model = ocr_model
        self.tts_model = tts_model
        self.summary_model = summary_model
        self.stt_model = stt_model
        self.qa_model = qa_model
        self.voice = voice
        self.state = ReaderState.IDLE
        self.session = None
        self.current_task = None
        self.generation = 0
        self.lock = threading.RLock()

    def _next_generation(self):
        self.generation += 1
        return self.generation

    def cancel_audio(self):
        with self.lock:
            if self.current_task:
                self.current_task.cancel_event.set()
                self.current_task = None
            self._next_generation()

    def close(self):
        with self.lock:
            self.cancel_audio()
            if self.session:
                self.session.ocr_cancel.set()
                with self.session.condition:
                    self.session.condition.notify_all()

    def _start_ocr(self, session):
        threading.Thread(target=self._ocr_worker, args=(session,), daemon=True).start()

    def _ocr_worker(self, session):
        buffer = ""
        try:
            for delta in stream_ocr(self.client, self.ocr_model, session.image_path):
                if session.ocr_cancel.is_set():
                    break
                buffer += delta
                while True:
                    match = re.search(r"[.!?…](?:\s+|$)|\n\s*\n", buffer)
                    if not match:
                        break
                    cut = match.end()
                    session.append_sentence(buffer[:cut])
                    buffer = buffer[cut:]
            if buffer.strip() and not session.ocr_cancel.is_set():
                session.append_sentence(buffer)
            session.mark_ocr_done()
        except Exception as exc:
            session.mark_ocr_done(exc)

    def _new_audio_task(self, kind, state, text=None):
        with self.lock:
            if self.current_task:
                self.current_task.cancel_event.set()
            task = AudioTask(self._next_generation(), kind, text=text)
            self.current_task = task
            self.state = state
            return task

    def load_image(self, image_path):
        image_path = Path(image_path)
        if not image_path.is_file():
            raise ReaderError(f"Image not found: {image_path}")
        with self.lock:
            if self.session:
                self.session.ocr_cancel.set()
            self.cancel_audio()
            self.session = DocumentSession(image_path)
            self.state = ReaderState.READING
            self._start_ocr(self.session)
            return self._new_audio_task("reading", ReaderState.READING)

    def button1(self, image_path=None):
        with self.lock:
            if self.state in {ReaderState.IDLE, ReaderState.READING, ReaderState.FINISHED}:
                if image_path is None:
                    raise ReaderError("A new image is required in the current state.")
                return self.load_image(image_path)
            if self.session is None:
                raise ReaderError("No active image.")
            if not self.session.has_unread():
                if image_path is None:
                    raise ReaderError("The image is finished; a new image is required.")
                return self.load_image(image_path)
            self.cancel_audio()
            return self._new_audio_task("reading", ReaderState.READING)

    def button2(self):
        with self.lock:
            if self.session is None:
                raise ReaderError("No active image to summarize.")
            self.cancel_audio()
            operation = self.generation
            self.state = ReaderState.SUMMARIZING
            source = self.session.source_text()
        if not source:
            with self.lock:
                self.state = ReaderState.PAUSED
            raise ReaderError("No spoken text is available to summarize.")
        try:
            summary = summarize_text(self.client, self.summary_model, source)
        except Exception:
            with self.lock:
                if operation == self.generation:
                    self.state = ReaderState.PAUSED
            raise
        if not summary:
            with self.lock:
                if operation == self.generation:
                    self.state = ReaderState.PAUSED
            raise ReaderError("The summary model returned no text.")
        with self.lock:
            if operation != self.generation or self.state != ReaderState.SUMMARIZING:
                return None
            return self._new_audio_task("summary", ReaderState.SUMMARIZING, summary)

    def button3_start(self):
        with self.lock:
            if self.session is None:
                raise ReaderError("No active image for questions.")
            if self.state == ReaderState.PROCESSING_QUESTION:
                return False
            self.cancel_audio()
            if not self.session.source_text():
                self.state = ReaderState.PAUSED
                raise ReaderError("No spoken text is available for questions.")
            self.state = ReaderState.RECORDING
            return True

    def button3_finish(self, audio_path):
        with self.lock:
            if self.session is None or self.state != ReaderState.RECORDING:
                raise ReaderError("Question recording is not active.")
            operation = self._next_generation()
            self.state = ReaderState.PROCESSING_QUESTION
            source = self.session.source_text()
        try:
            question = transcribe_question(self.client, self.stt_model, audio_path)
        except Exception:
            with self.lock:
                if operation == self.generation:
                    self.state = ReaderState.PAUSED
            raise
        if not question:
            with self.lock:
                if operation == self.generation:
                    self.state = ReaderState.PAUSED
            raise ReaderError("No question was recognized.")
        with self.lock:
            if operation != self.generation or self.state != ReaderState.PROCESSING_QUESTION:
                return None
        try:
            answer = answer_question(self.client, self.qa_model, source, question)
        except Exception:
            with self.lock:
                if operation == self.generation:
                    self.state = ReaderState.PAUSED
            raise
        if not answer:
            with self.lock:
                if operation == self.generation:
                    self.state = ReaderState.PAUSED
            raise ReaderError("The question model returned no answer.")
        with self.lock:
            if operation != self.generation or self.state != ReaderState.PROCESSING_QUESTION:
                return None
            self.session.latest_question = question
            self.session.latest_answer = answer
            return self._new_audio_task("answer", ReaderState.ANSWERING, answer)

    def status(self):
        with self.lock:
            session = self.session
            return {
                "state": self.state.value,
                "generation": self.current_task.generation if self.current_task else None,
                "can_resume": bool(session and session.has_unread()),
                "question": session.latest_question if session else None,
                "answer": session.latest_answer if session else None,
            }

    def iter_audio(self, task):
        try:
            if task.kind == "reading":
                yield from self._iter_reading(task)
            else:
                completed = False
                try:
                    for chunk in stream_tts(
                            self.client, self.tts_model, self.voice, task.text):
                        if task.cancel_event.is_set() or task.generation != self.generation:
                            return
                        yield chunk
                    completed = True
                finally:
                    if completed:
                        self._finish_aux_task(task)
        except Exception:
            with self.lock:
                if task.generation == self.generation:
                    self.current_task = None
                    self.state = ReaderState.ERROR
            raise

    def _iter_reading(self, task):
        session = self.session
        completed_stream = False
        try:
            while not task.cancel_event.is_set() and task.generation == self.generation:
                with session.condition:
                    while (session.cursor >= len(session.sentences)
                           and not session.ocr_done and not task.cancel_event.is_set()):
                        session.condition.wait(0.1)
                    if session.ocr_error:
                        raise session.ocr_error
                    if session.cursor >= len(session.sentences):
                        if session.ocr_done:
                            completed_stream = True
                        break
                    index = session.cursor
                    text = session.sentences[index]
                    session.started_index = index

                sentence_complete = False
                for chunk in stream_tts(
                        self.client, self.tts_model, self.voice, text):
                    if task.cancel_event.is_set() or task.generation != self.generation:
                        return
                    yield chunk
                else:
                    sentence_complete = True

                if sentence_complete:
                    with session.condition:
                        if session.cursor == index:
                            session.cursor += 1
                        session.started_index = None
        finally:
            if completed_stream:
                with self.lock:
                    if task.generation == self.generation:
                        self.current_task = None
                        self.state = ReaderState.FINISHED

    def _finish_aux_task(self, task):
        with self.lock:
            if task.generation != self.generation:
                return
            self.current_task = None
            self.state = (ReaderState.PAUSED if self.session.has_unread()
                          else ReaderState.FINISHED)
