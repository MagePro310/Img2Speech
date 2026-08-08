# ocr_vlm — Vietnamese page reader (OCR → Text-to-Speech)

Photograph a page of text (e.g. a Vietnamese web-novel chapter) and have it
read aloud with an OpenAI vision model for OCR and OpenAI TTS for the voice.
Built as the software for a Raspberry Pi reading device: snap a photo, hear
the recognized text read aloud.

## The four tools

| Script | Purpose | Output |
|---|---|---|
| `ocr_to_speech.py` | **Batch converter** — archive chapters as audio files | one `.mp4` (audio-only, AAC) + `.txt` transcript per image |
| `read_aloud.py` | **Live reader** — the device engine; speech streams as OCR sentences arrive | audio played directly through `aplay` (or any PCM player) |
| `device_reader.py` | **Three-button Pi controller** — capture, resumable reading, summary, and questions | continuous GPIO-driven audio |
| `serve_reader.py` | **Browser simulator** — test the same three-button workflow with a camera/file picker and microphone | streaming WAV plus the latest question/answer |

The persistent controller speaks one OCR sentence at a time. If it is
interrupted, its cursor stays at that sentence, so Button 1 resumes from the
start of the interrupted sentence. OCR continues in the background while a
summary or question is being handled.

The Raspberry Pi controller and browser simulator also speak short Vietnamese
status notices before and after reading, summaries, recording, and answers.
Notices use the selected TTS model/voice and are cached in memory after first use.

## Setup

Requires [uv](https://docs.astral.sh/uv/) (it fetches its own Python; system
Python is not used). Dependencies (`openai`, `imageio-ffmpeg`, `python-dotenv`,
`gpiozero`) are declared in `pyproject.toml`:

```bash
uv sync
```

Put your OpenAI API key in a `.env` file next to the scripts:

```
OPENAI_API_KEY=sk-...
```

`.env` is loaded automatically. **Never commit `.env`** — if this folder
becomes a git repo, add it to `.gitignore` first, and keep only a placeholder
value in `.env.example`.

## Usage

```bash
# Batch: every image becomes <name>.mp4 + <name>.txt next to it
uv run ocr_to_speech.py chapter1.jpg chapter2.jpg     # files or a directory

# Live: read one photo aloud through the default ALSA player (aplay)
uv run read_aloud.py photo.jpg

# Live without a sound device (testing): capture raw PCM instead
uv run read_aloud.py photo.jpg --pcm-out out.pcm

# Raspberry Pi: stop and summarize with a button on BCM GPIO 17
uv run read_aloud.py photo.jpg --summary-button-pin

# Use a different BCM pin
uv run read_aloud.py photo.jpg --summary-button-pin 27

# Persistent Raspberry Pi reader (defaults: Button 1/2/3 = BCM 17/27/22)
uv run device_reader.py \
  --capture-command 'camera-wrapper --output {output}' \
  --record-command 'recorder-wrapper --output {output}'

# Three-button browser simulator, then open http://localhost:8765
uv run serve_reader.py
```

Common model flags where applicable: `--voice` (default `marin` for all spoken
output),
`--ocr-model` (default `gpt-5.6-sol` for batch, live, Raspberry Pi, and web),
`--tts-model` (default `gpt-4o-mini-tts`).

The default OCR path is quality-first: it sends the image with `detail: "high"`
and uses `reasoning_effort: "max"` with `gpt-5.6-sol`. A manually selected
`--ocr-model` still receives the high-detail image, but the Sol-specific
reasoning setting is omitted for compatibility. There is no automatic OCR
fallback; an unavailable or failed model is reported through the existing error
path. Use `--ocr-model <model-id>` explicitly if the project must use another
vision-capable model.

The `marin` voice is supported by the default `gpt-4o-mini-tts` model. If you
select `tts-1` or `tts-1-hd`, also select a voice supported by that model.

The interactive readers accept `--summary-model` (default `gpt-4o-mini`).
`device_reader.py` and `serve_reader.py` also accept `--stt-model` (default
`gpt-4o-mini-transcribe`) and `--qa-model` (default `gpt-4o-mini`). Each
question sends only the original text heard so far and the current recognized
question. Previous questions and answers are never sent to the model.

On the browser page:

- Button 1 opens the camera/file picker when a new image is needed; otherwise
  it resumes the current image.
- Button 2 stops the current audio and creates a new summary from all original
  sentences heard completely so far. The first interrupted sentence is used
  as a fallback when no sentence has finished.
- Button 3 starts microphone recording; press it again to stop, transcribe, and
  answer using only the text heard from the current image.

On the Raspberry Pi, press Button 1 briefly to keep its normal capture/resume
behavior. Hold Button 1 for 1.5 seconds to discard the current page in any state,
capture a new page, and start reading it automatically. The long press emits only
the new-page action; releasing the button does not also resume the discarded page.

JPEG/JPG and PNG uploads are supported and limited to 20 MiB. Browser sessions live in memory only, and the
server retains at most 32 sessions.

The browser simulator exposes `POST /button/1`, `/button/2`, `/button/3`,
`GET /audio`, and `GET /status`, all keyed by the `sid` query parameter.
Button 1 accepts JPEG or PNG data for a new image or an empty body to resume. Button 3
uses an empty body to begin recording and WAV, WebM, or Ogg data to finish it.
When Button 2 or Button 3 interrupts browser narration, the client also sends
the optional `generation` and `played_seconds` query parameters so the server
resumes from the first sentence that was not completely heard.

## How the live streaming works

The point of `read_aloud.py` is that the user should not wait for the whole
pipeline before hearing anything. Every stage streams, and the stages overlap:

```
 OCR (vision model,      sentence            TTS (one stream per      audio player
 stream=True)            segmenter           segment, raw PCM)        (aplay)
 tokens arrive     ──►   1st sentence   ──►  chunks arrive as    ──►  one long-lived
 as generated            emitted ASAP;       synthesized              process; PCM is
                         later ones          (up to 2 segments        gapless so
                         coalesce to         streaming                segments join
                         ~300 chars          concurrently)            seamlessly
```

1. **Streaming OCR** — the image goes to the vision model once; text tokens
   stream back and accumulate in a buffer (`segment_worker`).
2. **Segmentation** — the *first* complete sentence is emitted immediately so
   audio can start as early as possible; later sentences are coalesced to
   ~300 characters for better prosody (`take_segment`).
3. **Streaming TTS** — each segment's TTS request opens *as soon as its text
   exists*, up to two concurrently, each pouring raw PCM (24 kHz, 16-bit,
   mono) into its own bounded buffer (`tts_worker` + `pump`). Starting
   segment N+1's stream while N is still playing is what prevents silent
   gaps between sentences.
4. **Playback** — the main thread drains the buffers strictly in order into a
   single player process. Raw PCM has no container or decoder state, so
   consecutive segments concatenate gaplessly, and the player's blocking
   stdin naturally paces the whole pipeline at real-time speed.

Time to first audio depends on the image, network, model access, and reasoning
work. The `gpt-5.6-sol` high-detail, maximum-reasoning default favors OCR
quality and may start more slowly than earlier live defaults. Every run prints
timestamped milestones (first OCR token, first audio byte, per-segment
completion) so latency regressions are easy to spot.

The batch script instead maximizes archive quality: full OCR, then TTS in
≤3500-char chunks returned as AAC, concatenated and remuxed without
re-encoding into an audio-only `.mp4` (ffmpeg comes bundled via
`imageio-ffmpeg` — no system install).

`serve_reader.py` uses the persistent sentence-level controller and pipes PCM
into an HTTP response with a WAV header of unknown length, which browsers play
progressively. It binds to `127.0.0.1` only; reach it through VS Code port
forwarding (Ports panel → forward `8765`).

In the persistent controller, each TTS request corresponds to one OCR sentence.
Interrupting playback cancels that audio generation but leaves OCR running.
Only a fully completed sentence advances the cursor; if the first sentence is
interrupted, it is still available as the summary/question fallback. Loading a
new image is the only action that cancels and replaces the OCR session.

## Model choice, quality, latency & cost

- OCR defaults to [`gpt-5.6-sol`](https://developers.openai.com/api/docs/models/gpt-5.6-sol)
  in every workflow. High image detail plus maximum reasoning prioritizes
  transcription quality, but can increase time to first audio and API spend.
  Confirm that the OpenAI project has access to this model before deployment
  and consult current account pricing rather than relying on a fixed estimate.
  The application does not silently fall back when OCR fails; choose another
  vision-capable model explicitly with `--ocr-model <model-id>` if needed.
- Spoken page text, summaries, answers, and notices continue to use
  `gpt-4o-mini-tts` with the `marin` voice by default. Summary and Q&A use
  `gpt-4o-mini`, while question transcription uses
  `gpt-4o-mini-transcribe`.
- The TTS narration style is steered by `TTS_INSTRUCTIONS` in
  `ocr_to_speech.py` ("natural, fluent Vietnamese narration") — edit it to
  change pace or language.

## Deploying on the Raspberry Pi

Tài liệu tiếng Việt:

- [Hướng dẫn vận hành hằng ngày](RASPBERRY_PI_VAN_HANH_VI.md) — khởi động,
  thao tác ba nút, đổi trang, tín hiệu âm thanh và xử lý lỗi nhanh.
- [Hướng dẫn triển khai đầy đủ](RASPBERRY_PI_DEPLOY_VI.md) — sơ đồ nối nút,
  camera, microphone, ALSA và service systemd.

1. Copy this folder (including `.env`) to the Pi and install uv; `uv sync`
   fetches an ARM Python automatically.
2. `aplay` ships with Raspberry Pi OS — verify the speaker with `aplay -l`.
3. Connect three momentary buttons from three distinct BCM GPIO pins to GND.
   All inputs use internal pull-ups and 100 ms debounce, so no external
   resistors are required. The defaults are Button 1 = GPIO 17, Button 2 =
   GPIO 27, and Button 3 = GPIO 22; use the three `--button*-pin` flags to
   override them with distinct BCM numbers.
4. Provide capture and recording wrapper commands. They are parsed with
   `shlex` and executed directly, never through a shell. Each must contain the
   literal `{output}` placeholder. Capture must create a JPEG within 30 seconds;
   recording must create a WAV. Recording stops on the second Button 3 press
   (SIGINT, then terminate/kill fallback) or automatically after 60 seconds.
5. Start the controller with the `device_reader.py` command shown above.
   Button 1 captures/reads or resumes, Button 2 stops/summarizes, and Button 3
   toggles a question recording. One local beep marks recording start, two mark
   recording stop, and a separate low pattern reports errors.
6. A different audio player can be substituted with
   `--player "mpv --demuxer=rawaudio ..."` etc. (it must accept raw PCM on
   stdin).
