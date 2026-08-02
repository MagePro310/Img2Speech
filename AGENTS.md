# AGENTS.md

## Scope

These instructions apply to the entire repository. Work from the repository root,
keep changes focused, and preserve unrelated or pre-existing worktree changes.

## Project overview

Img2Speech is a Python 3.10+ OCR-to-speech project managed with `uv`. It uses
top-level scripts rather than an installable application package:

- `ocr_to_speech.py` performs batch OCR, writes transcripts, synthesizes AAC in
  chunks, and remuxes the result into audio-only MP4 files.
- `read_aloud.py` implements the low-latency streaming OCR, sentence segmentation,
  concurrent TTS, and ordered raw-PCM playback pipeline.
- `reader_controller.py` owns the shared resumable reader state machine used by
  both interactive front ends.
- `device_reader.py` adapts the controller to Raspberry Pi GPIO buttons, a camera
  command, a recorder command, and a PCM player.
- `serve_reader.py` provides the localhost browser simulator with an inline web UI
  and in-memory sessions.
- `spoken_notices.py` provides shared Vietnamese status notices, a thread-safe PCM
  cache, and beep fallbacks.
- `tests/` contains the offline `unittest` suite.

Put shared reading behavior in the streaming or controller modules. Keep hardware
and HTTP details in their adapters so the GPIO and browser workflows do not drift.

## Setup and verification

- Install or synchronize dependencies with `uv sync`.
- Run the complete suite from the repository root:

  ```bash
  uv run python -m unittest discover -s tests -v
  ```

- Run one test file while iterating:

  ```bash
  uv run python -m unittest discover -s tests -p 'test_controller.py' -v
  ```

- If the default `uv` cache is read-only in a sandbox, use a writable temporary
  cache:

  ```bash
  UV_CACHE_DIR=/tmp/img2speech-uv-cache uv run python -m unittest discover -s tests -v
  ```

- Smoke-test CLI parsing with `uv run <script>.py --help`; the public scripts are
  `ocr_to_speech.py`, `read_aloud.py`, `device_reader.py`, and `serve_reader.py`.
- There is no configured formatter, linter, static type checker, or CI workflow.
  Do not invent a mandatory check or mass-format unrelated code.

Tests must stay fast and offline. Mock OpenAI calls, GPIO, camera and recorder
subprocesses, audio players, and browser I/O. Tests must not require an API key,
network access, an audio device, or Raspberry Pi hardware.

## Code conventions

- Follow the existing PEP 8-style Python: four-space indentation, `snake_case`
  functions and variables, `PascalCase` classes, and uppercase module constants.
- Prefer `pathlib.Path`, small helpers, dataclasses for state, explicit resource
  cleanup, and `ReaderError` for recoverable input or workflow failures.
- Keep CLI parsing in `main()` and retain the `if __name__ == "__main__":` guard.
- Preserve Vietnamese diacritics and established user-facing prompt and notice
  wording unless the requested product behavior changes it.
- Preserve existing public CLI flags, HTTP shapes, and controller behavior unless
  the task explicitly calls for an interface change.
- When dependencies change, update both `pyproject.toml` and `uv.lock`.
- Update `README.md` for user-visible behavior. Also update
  `RASPBERRY_PI_DEPLOY_VI.md` for GPIO, camera, microphone, audio, or service
  deployment changes.

## Behavioral invariants

- A reading cursor advances only after an original sentence has been played in
  full. An interrupted sentence must resume from its beginning. If the first
  sentence is interrupted before any sentence completes, that started sentence is
  the summary and question fallback.
- Summaries use only original source text heard so far; never feed an earlier
  summary back into the source. A question uses only that original source text and
  the current recognized question; never add previous questions or answers as
  model history.
- Spoken status notices and beeps are presentation audio. They must not advance the
  source cursor or enter summary or question context.
- Generation IDs and cancellation events prevent stale OCR, TTS, summary, or
  question work from overwriting a newer action. Avoid holding controller locks
  during slow model calls. Audio interruption leaves OCR running; loading a new
  image cancels and replaces the prior OCR session.
- Preserve streaming latency, ordered playback, bounded buffering, thread
  synchronization, and prompt cancellation. TTS may be produced concurrently, but
  audio must be consumed in source order.
- Raw TTS PCM is 24 kHz, signed 16-bit, mono. Any format change must update the
  player command, byte-to-time calculations, WAV header, browser streaming, and
  related tests together.
- Browser playback reconciliation requires `generation` and `played_seconds`
  together, ignores stale generations, and rewinds to the first sentence not fully
  heard.

## Resource, security, and data guardrails

- Never read, print, edit, or commit `.env`; it may contain `OPENAI_API_KEY`. Keep
  only placeholder values in `.env.example`.
- Do not make real OpenAI requests unless explicitly requested. They send user
  images, audio, or text to an external service and can incur cost.
- Do not commit generated transcripts, audio, camera captures, question recordings,
  virtual environments, caches, or bytecode. Use temporary files/directories and
  clean them on success, cancellation, and failure. Do not replace the tracked
  `sample_input.jpg` fixture unintentionally.
- Preserve subprocess safety: require the literal `{output}` placeholder, parse
  commands with `shlex.split`, pass argument lists directly, never enable
  `shell=True`, retain timeouts, validate non-empty output and JPEG/WAV signatures,
  and terminate or kill child processes when graceful shutdown fails.
- Close GPIO buttons, streams, players, recorders, threads, and temporary sessions
  deterministically. Cancellation and error paths require the same cleanup as the
  successful path.
- Keep the browser server bound to `127.0.0.1`. Preserve session-ID validation,
  MIME and file-signature allowlists, one-shot audio generations, no-store audio
  responses, the 20 MiB upload limit, the 32-session cap, and expired-session
  cleanup. Do not expose the simulator publicly without an explicit authentication
  and security design.
- Do not run Raspberry Pi device workflows on ordinary development machines.
  Hardware acceptance is manual and follows `RASPBERRY_PI_DEPLOY_VI.md`; deployment
  services must not run as root.

## Test expectations

Add regression coverage for changes involving state transitions, cancellation,
cursor/resume semantics, stale generations, summary or Q&A source isolation,
notice/beep ordering, subprocess validation, web upload limits, playback progress,
or session cleanup. Run the full offline suite before handoff and clearly identify
any hardware-only check that was not performed.
