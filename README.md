# ocr_vlm — Vietnamese page reader (OCR → Text-to-Speech)

Photograph a page of text (e.g. a Vietnamese web-novel chapter) and have it
read aloud with an OpenAI vision model for OCR and OpenAI TTS for the voice.
Built as the software for a Raspberry Pi reading device: snap a photo, hear
the text a few seconds later.

## The three tools

| Script | Purpose | Output |
|---|---|---|
| `ocr_to_speech.py` | **Batch converter** — archive chapters as audio files | one `.mp4` (audio-only, AAC) + `.txt` transcript per image |
| `read_aloud.py` | **Live reader** — the device engine; speech starts ~3s after the photo | audio played directly through `aplay` (or any PCM player) |
| `serve_reader.py` | **Browser test page** — hear the live reader from a laptop when the host has no speakers | streaming WAV played in the browser via a forwarded port |

## Setup

Requires [uv](https://docs.astral.sh/uv/) (it fetches its own Python; system
Python is not used). Dependencies (`openai`, `imageio-ffmpeg`, `python-dotenv`)
are declared in `pyproject.toml`:

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

# Browser test: serve a click-to-listen page, then open http://localhost:8765
uv run serve_reader.py
```

Common flags on all three: `--voice` (default `onyx`, deep male),
`--ocr-model` (default `gpt-4o` for batch, `gpt-4o-mini` for live),
`--tts-model` (default `gpt-4o-mini-tts`).

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

Measured on the included `sample_input.jpg` (921 chars of Vietnamese prose):
first audio at **~3.3–4.6 s** (vs ~15 s for the sequential batch pipeline),
then 65 s of continuous narration with no audible gaps. Every run prints
timestamped milestones (first OCR token, first audio byte, per-segment
completion) so regressions are easy to spot.

The batch script instead maximizes archive quality: full OCR, then TTS in
≤3500-char chunks returned as AAC, concatenated and remuxed without
re-encoding into an audio-only `.mp4` (ffmpeg comes bundled via
`imageio-ffmpeg` — no system install).

`serve_reader.py` reuses `segment_worker`/`tts_worker` unchanged and pipes
the same PCM into an HTTP response with a WAV header of unknown length,
which browsers play progressively. It binds to `127.0.0.1` only; reach it
through VS Code port forwarding (Ports panel → forward `8765`).

## Model choice & cost

- OCR: `gpt-4o` transcribed the sample perfectly; `gpt-4o-mini` (live
  default) is ~15× cheaper and faster but made ~9 Vietnamese diacritic
  errors on the same page (≈98.7 % match) — audible as occasional
  mispronounced words. If that bothers you: `--ocr-model gpt-4o`.
- TTS `gpt-4o-mini-tts` costs ≈ $0.015 per minute of audio; a typical page
  is **~$0.02 per reading** all-in.
- The TTS narration style is steered by `TTS_INSTRUCTIONS` in
  `ocr_to_speech.py` ("natural, fluent Vietnamese narration") — edit it to
  change pace or language.

## Deploying on the Raspberry Pi

1. Copy this folder (including `.env`) to the Pi and install uv; `uv sync`
   fetches an ARM Python automatically.
2. `aplay` ships with Raspberry Pi OS — verify the speaker with `aplay -l`.
3. Wire your camera/button to run `uv run read_aloud.py <photo>` per shot.
   A different audio player can be substituted with
   `--player "mpv --demuxer=rawaudio ..."` etc. (it must accept raw PCM on
   stdin).
