#!/usr/bin/env python
"""OCR .jpg images with an OpenAI vision model, then read the text aloud
via OpenAI TTS into audio-only .mp4 files (one per image).

Reads OPENAI_API_KEY from a .env file next to this script (or the environment).

Usage:
    uv run ocr_to_speech.py sample_input.jpg [more.jpg ...] [--voice marin]
"""

import argparse
import base64
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import imageio_ffmpeg
from dotenv import load_dotenv
from openai import OpenAI

OCR_PROMPT = (
    "Transcribe all visible text in this image exactly as written. "
    "Preserve Vietnamese diacritics and paragraph breaks. "
    "Output only the transcribed text, nothing else."
)
DEFAULT_OCR_MODEL = "gpt-5.6-sol"
DEFAULT_OCR_IMAGE_DETAIL = "high"
DEFAULT_OCR_REASONING_EFFORT = "low"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_VOICE = "marin"
TTS_INSTRUCTIONS = (
    "Read the text aloud as natural, fluent Vietnamese narration "
    "at a comfortable storytelling pace."
)
MAX_TTS_CHARS = 3500  # API limit is 4096 per request; leave headroom


def ocr_reasoning_kwargs(model):
    """Return reasoning options supported by the quality-first OCR default."""
    if model == DEFAULT_OCR_MODEL:
        return {"reasoning_effort": DEFAULT_OCR_REASONING_EFFORT}
    return {}


def ocr_image(client, model, image_path):
    mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    resp = client.chat.completions.create(
        model=model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": OCR_PROMPT},
                {"type": "image_url", "image_url": {
                    "url": f"data:{mime};base64,{b64}",
                    "detail": DEFAULT_OCR_IMAGE_DETAIL,
                }},
            ],
        }],
        **ocr_reasoning_kwargs(model),
    )
    return resp.choices[0].message.content.strip()


def chunk_text(text, limit=MAX_TTS_CHARS):
    """Split into chunks <= limit chars, preferring paragraph then sentence breaks."""
    pieces = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        for sent in re.split(r"(?<=[.!?…])\s+", para) if len(para) > limit else [para]:
            while len(sent) > limit:  # last resort: hard split
                pieces.append(sent[:limit])
                sent = sent[limit:]
            if sent:
                pieces.append(sent)

    chunks, current = [], ""
    for piece in pieces:
        if current and len(current) + len(piece) + 2 > limit:
            chunks.append(current)
            current = piece
        else:
            current = f"{current}\n\n{piece}" if current else piece
    if current:
        chunks.append(current)
    return chunks


def synthesize(client, model, voice, text):
    # `instructions` is only accepted by the gpt-* TTS models, not tts-1/tts-1-hd
    kwargs = {"instructions": TTS_INSTRUCTIONS} if model.startswith("gpt-") else {}
    resp = client.audio.speech.create(
        model=model, voice=voice, input=text, response_format="aac", **kwargs
    )
    return resp.content


def write_mp4(aac_bytes, out_path):
    """Remux raw ADTS/AAC bytes into an audio-only .mp4 (no re-encode)."""
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.NamedTemporaryFile(suffix=".aac", delete=False) as tmp:
        tmp.write(aac_bytes)
        tmp_path = tmp.name
    try:
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", tmp_path,
             "-c", "copy", "-bsf:a", "aac_adtstoasc", str(out_path)],
            check=True,
        )
    finally:
        os.unlink(tmp_path)


def collect_images(inputs):
    images = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            images.extend(sorted(p.glob("*.jpg")) + sorted(p.glob("*.jpeg")))
        elif p.is_file():
            images.append(p)
        else:
            sys.exit(f"Input not found: {item}")
    return images


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("inputs", nargs="+", help=".jpg files or directories containing them")
    ap.add_argument(
        "--voice", default=DEFAULT_VOICE,
        help=f"TTS voice (default: {DEFAULT_VOICE})",
    )
    ap.add_argument("--ocr-model", default=DEFAULT_OCR_MODEL, help="vision model for OCR")
    ap.add_argument("--tts-model", default=DEFAULT_TTS_MODEL, help="TTS model")
    ap.add_argument("--outdir", type=Path, help="output directory (default: next to each input)")
    args = ap.parse_args()

    load_dotenv(Path(__file__).with_name(".env"))
    if not os.environ.get("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Put it in .env next to this script or export it.")
    client = OpenAI()

    images = collect_images(args.inputs)
    if not images:
        sys.exit("No input images found.")
    if args.outdir:
        args.outdir.mkdir(parents=True, exist_ok=True)

    failures = []
    t_batch = time.perf_counter()
    for image in images:
        outdir = args.outdir or image.parent
        t_img = time.perf_counter()
        try:
            print(f"[{image.name}] OCR with {args.ocr_model} ...")
            t = time.perf_counter()
            text = ocr_image(client, args.ocr_model, image)
            txt_path = outdir / f"{image.stem}.txt"
            txt_path.write_text(text + "\n", encoding="utf-8")
            chunks = chunk_text(text)
            print(f"[{image.name}] {len(text)} chars -> {txt_path.name}, "
                  f"{len(chunks)} TTS chunk(s) (OCR {time.perf_counter() - t:.1f}s)")
            audio = b""
            for i, chunk in enumerate(chunks, 1):
                t = time.perf_counter()
                audio += synthesize(client, args.tts_model, args.voice, chunk)
                print(f"[{image.name}] TTS chunk {i}/{len(chunks)} "
                      f"({args.tts_model}, voice={args.voice}) {time.perf_counter() - t:.1f}s")
            out_path = outdir / f"{image.stem}.mp4"
            t = time.perf_counter()
            write_mp4(audio, out_path)
            print(f"[{image.name}] wrote {out_path} "
                  f"(mux {time.perf_counter() - t:.1f}s, total {time.perf_counter() - t_img:.1f}s)")
        except Exception as e:
            print(f"[{image.name}] FAILED: {e}", file=sys.stderr)
            failures.append(image.name)

    if failures:
        sys.exit(f"{len(failures)} of {len(images)} image(s) failed: {', '.join(failures)}")
    print(f"Done: {len(images)} image(s) processed in {time.perf_counter() - t_batch:.1f}s.")


if __name__ == "__main__":
    main()
