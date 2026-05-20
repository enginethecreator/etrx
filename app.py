import os
import re
import json
import uuid
import math
import threading
import subprocess
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from flask import Flask, render_template, request, jsonify, send_file, Response

# ─── App Config ───────────────────────────────────────────────────────────────
app = Flask(title="Try", version="0.0.2")
# app.config["MAX_CONTENT_LENGTH"] = 1000 * 1024 * 1024  # 500M
BASE_DIR = Path(os.environ.get("BASE_DIR", "./downloads"))
BASE_DIR.mkdir(parents=True, exist_ok=True)

UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
TEMP_DIR   = BASE_DIR / "temp"

for d in [UPLOAD_DIR, OUTPUT_DIR, TEMP_DIR]:
    d.mkdir(exist_ok=True)

# In-memory job state  { job_id: { status, progress, message, result } }
JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()

# ─── Helpers ──────────────────────────────────────────────────────────────────

def job_update(job_id: str, **kwargs):
    with JOBS_LOCK:
        JOBS[job_id].update(kwargs)


def seconds_to_ts(s: float) -> str:
    """Float seconds → HH:MM:SS.mmm"""
    s = max(0.0, round(s, 3))
    h  = int(s // 3600)
    m  = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"


def ts_to_seconds(ts: str) -> float:
    """HH:MM:SS.mmm → float seconds"""
    parts = ts.split(":")
    h, m, s = float(parts[0]), float(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def merge_segments_to_transcript(segments: list[dict]) -> list[dict]:
    """
    Merge overlapping word-group segments into sentence-level chunks.
    Each output entry: { start: HH:MM:SS.mmm, end: HH:MM:SS.mmm, text: str }
    Uses sentence-ending punctuation + minimum 3s duration as flush gate.
    """
    sentence_enders = {'.', '?', '!', '"'}
    merged = []
    current = None

    for seg in segments:
        raw_start = seg.get("start", 0)
        duration  = seg.get("duration", 2)
        raw_end   = raw_start + duration
        text      = seg.get("text", "").strip()

        if current is None:
            current = {"start": raw_start, "end": raw_end, "text": text}
            continue

        overlap        = raw_start < current["end"]
        last_char      = current["text"].rstrip()[-1] if current["text"].rstrip() else ""
        ends_sentence  = last_char in sentence_enders
        chunk_duration = current["end"] - current["start"]

        if ends_sentence and not overlap and chunk_duration >= 3.0:
            merged.append(current)
            current = {"start": raw_start, "end": raw_end, "text": text}
        else:
            current["end"]  = max(current["end"], raw_end)
            current["text"] = current["text"] + " " + text

    if current:
        merged.append(current)

    return [
        {
            "start": seconds_to_ts(s["start"]),
            "end":   seconds_to_ts(s["end"]),
            "text":  s["text"].strip()
        }
        for s in merged
    ]


# ─── Route: Index ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ─── Route: Extract subtitle/info from YouTube URL ────────────────────────────

@app.route("/api/extract", methods=["POST"])
def extract():
    data = request.json or {}
    url  = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "progress": 0, "message": "Starting extraction…", "result": None}

    thread = threading.Thread(target=_extract_worker, args=(job_id, url), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


def _extract_worker(job_id: str, url: str):
    try:
        work_dir = TEMP_DIR / job_id
        work_dir.mkdir(exist_ok=True)

        # ── Step 1: pull video metadata ───────────────────────────────────────
        job_update(job_id, progress=10, message="Fetching video metadata…")
        meta_cmd = [
            "yt-dlp",
            "--dump-json",
            "--no-playlist",
            url
        ]
        meta_result = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_result.returncode != 0:
            raise RuntimeError(f"yt-dlp metadata failed: {meta_result.stderr[:300]}")

        meta = json.loads(meta_result.stdout)
        video_id = meta.get("id", "unknown")
        title    = meta.get("title", "Unknown Title")
        duration = meta.get("duration", 0)
        channel  = meta.get("uploader", "")
        thumb    = meta.get("thumbnail", "")

        # ── Step 2: download subtitles (auto-generated preferred) ────────────
        job_update(job_id, progress=30, message="Downloading subtitles / auto-captions…")
        sub_base = str(work_dir / f"sub_{video_id}")
        sub_cmd  = [
            "yt-dlp",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en.*",
            "--sub-format", "json3",
            "--skip-download",
            "--output", sub_base,
            "--no-playlist",
            url
        ]
        subprocess.run(sub_cmd, capture_output=True, text=True, timeout=120)

        # find the downloaded json3 file
        json3_files = list(work_dir.glob("*.json3"))
        segments    = []
        full_text   = ""

        if json3_files:
            job_update(job_id, progress=55, message="Parsing subtitle JSON3…")
            with open(json3_files[0], encoding="utf-8") as f:
                j3 = json.load(f)

            segments, full_text = _parse_json3(j3)
        else:
            # Fallback: try VTT
            vtt_cmd = [
                "yt-dlp",
                "--write-auto-subs",
                "--write-subs",
                "--sub-langs", "en.*",
                "--sub-format", "vtt",
                "--skip-download",
                "--output", sub_base,
                "--no-playlist",
                url
            ]
            subprocess.run(vtt_cmd, capture_output=True, text=True, timeout=120)
            vtt_files = list(work_dir.glob("*.vtt"))

            if vtt_files:
                job_update(job_id, progress=55, message="Parsing VTT subtitles…")
                segments, full_text = _parse_vtt(vtt_files[0])
            else:
                raise RuntimeError(
                    "No subtitles found. The video may not have auto-generated captions, "
                    "or captions are disabled. Try a different video."
                )

        # ── Step 3: build structured transcript ───────────────────────────────
        job_update(job_id, progress=80, message="Building structured transcript…")
        structured = merge_segments_to_transcript(segments)

        job_update(
            job_id,
            status="done",
            progress=100,
            message="Extraction complete.",
            result={
                "video_id":   video_id,
                "title":      title,
                "duration":   duration,
                "channel":    channel,
                "thumbnail":  thumb,
                "full_text":  full_text,
                "segments":   segments,          # raw { start, duration, text }
                "transcript": structured,        # formatted { start, end, text }
            }
        )

    except Exception as e:
        job_update(job_id, status="error", progress=0, message=str(e), result=None)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _parse_json3(j3: dict) -> tuple[list, str]:
    """Parse YouTube JSON3 subtitle format into segments + full_text."""
    segments  = []
    all_words = []

    events = j3.get("events", [])
    for event in events:
        t_start_ms = event.get("tStartMs", 0)
        segs        = event.get("segs", [])
        if not segs:
            continue

        text_parts = []
        for seg in segs:
            t_offset = seg.get("tOffsetMs", 0)
            utf8     = seg.get("utf8", "").replace("\n", " ").strip()
            if utf8:
                text_parts.append(utf8)

        text = " ".join(text_parts).strip()
        if not text:
            continue

        d_ms = event.get("dDurationMs", 2000)
        start_s    = t_start_ms / 1000.0
        duration_s = d_ms / 1000.0

        segments.append({"start": start_s, "duration": duration_s, "text": text})
        all_words.append(text)

    full_text = " ".join(all_words)
    # Clean up multiple spaces
    full_text = re.sub(r"\s+", " ", full_text).strip()
    return segments, full_text


def _parse_vtt(vtt_path: Path) -> tuple[list, str]:
    """Parse VTT subtitle file into segments + full_text."""
    inline_tag = re.compile(r"<[^>]+>")
    time_re    = re.compile(r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})")

    segments  = []
    all_text  = []
    current_start = None
    current_end   = None
    buf           = []

    with open(vtt_path, encoding="utf-8") as f:
        lines = f.readlines()

    def flush():
        nonlocal current_start, current_end, buf
        if buf and current_start is not None:
            text = " ".join(buf).strip()
            text = inline_tag.sub("", text).strip()
            if text:
                dur = ts_to_seconds(current_end) - ts_to_seconds(current_start)
                segments.append({
                    "start":    ts_to_seconds(current_start),
                    "duration": max(dur, 0.5),
                    "text":     text
                })
                all_text.append(text)
        current_start = current_end = None
        buf.clear()

    for line in lines:
        line = line.rstrip()
        m = time_re.search(line)
        if m:
            flush()
            current_start = m.group(1)
            current_end   = m.group(2)
        elif line and not line.startswith("WEBVTT") and not line.startswith("Kind:") \
                and not line.startswith("Language:") and not line.isdigit():
            if current_start:
                clean = inline_tag.sub("", line).strip()
                if clean:
                    buf.append(clean)

    flush()
    full_text = re.sub(r"\s+", " ", " ".join(all_text)).strip()
    return segments, full_text


# ─── Route: Job status poll ───────────────────────────────────────────────────

@app.route("/api/job/<job_id>")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job)


# ─── Route: Render clip from blueprint ────────────────────────────────────────

@app.route("/api/render", methods=["POST"])
def render_clip():
    data      = request.json or {}
    blueprint = data.get("blueprint")
    video_url = data.get("video_url", "").strip()

    if not blueprint:
        return jsonify({"error": "No blueprint provided"}), 400

    clip = blueprint  # single clip object from the schema
    content_type = clip.get("content_generation_type", "SLICED_FROM_SOURCE")

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running", "progress": 0, "message": "Queued…", "result": None}

    if content_type == "SLICED_FROM_SOURCE":
        if not video_url:
            return jsonify({"error": "video_url required for SLICED_FROM_SOURCE"}), 400
        thread = threading.Thread(
            target=_render_sliced_worker,
            args=(job_id, video_url, clip),
            daemon=True
        )
    else:
        # SYNTHETIC_FROM_SCRATCH — image + audio provided by user
        images_b64 = data.get("images", [])   # list of base64 data-URIs
        audio_path = data.get("audio_path", "")  # path on server OR upload
        thread = threading.Thread(
            target=_render_synthetic_worker,
            args=(job_id, clip, images_b64, audio_path),
            daemon=True
        )

    thread.start()
    return jsonify({"job_id": job_id})


# ─── Render Worker: SLICED_FROM_SOURCE ───────────────────────────────────────

def _render_sliced_worker(job_id: str, video_url: str, clip: dict):
    work_dir = TEMP_DIR / job_id
    work_dir.mkdir(exist_ok=True)

    try:
        ts_start = clip.get("timestamp_start", "00:00:00.000")
        ts_end   = clip.get("timestamp_end",   "00:00:30.000")
        platform = clip.get("target_platform", "YouTube Shorts")
        hook     = clip.get("hook_text_overlay", "")
        meta     = clip.get("publishing_metadata", {})
        title    = meta.get("title", "clip")

        job_update(job_id, progress=5, message="Downloading source video…")

        raw_path = str(work_dir / "raw_source.mp4")
        dl_cmd   = [
            "yt-dlp",
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
            "--merge-output-format", "mp4",
            "--output", raw_path,
            "--no-playlist",
            video_url
        ]
        result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"Download failed: {result.stderr[:300]}")

        if not Path(raw_path).exists():
            # yt-dlp may have added extension
            candidates = list(work_dir.glob("raw_source.*"))
            if candidates:
                raw_path = str(candidates[0])
            else:
                raise RuntimeError("Downloaded file not found after yt-dlp.")

        job_update(job_id, progress=50, message="Slicing and encoding clip…")

        safe_title  = re.sub(r'[^\w\-]', '_', title)[:40]
        output_name = f"{safe_title}_{job_id[:8]}.mp4"
        output_path = str(OUTPUT_DIR / output_name)

        # Compute duration for hook banner (5s cap from schema)
        start_sec   = ts_to_seconds(ts_start)
        end_sec     = ts_to_seconds(ts_end)
        clip_dur    = end_sec - start_sec

        # Build vf filter: crop to 9:16, scale to 1080x1920, optional hook text
        vf_parts = [
            "crop=ih*9/16:ih",
            "scale=1080:1920:flags=lanczos"
        ]

        if hook:
            hook_escaped = hook.replace("'", "\\'").replace(":", "\\:")
            hook_duration = min(5.0, clip_dur)
            # Bold white text with black outline at top of frame
            vf_parts.append(
                f"drawtext=text='{hook_escaped}'"
                f":fontsize=52"
                f":fontcolor=white"
                f":bordercolor=black"
                f":borderw=3"
                f":x=(w-text_w)/2"
                f":y=140"
                f":enable='between(t,0,{hook_duration})'"
            )

        vf_string = ",".join(vf_parts)

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-ss", ts_start,
            "-i", raw_path,
            "-to", str(clip_dur),
            "-vf", vf_string,
            "-c:v", "libx264", "-preset", "fast",
            "-profile:v", "main", "-level:v", "4.0",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            output_path
        ]

        ff_result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=300)
        if ff_result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {ff_result.stderr[-500:]}")

        job_update(
            job_id,
            status="done",
            progress=100,
            message="Clip rendered successfully.",
            result={
                "filename":    output_name,
                "download_url": f"/api/download/{output_name}",
                "title":       title,
                "platform":    platform,
                "duration":    round(clip_dur, 2)
            }
        )

    except Exception as e:
        job_update(job_id, status="error", progress=0, message=str(e))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ─── Render Worker: SYNTHETIC_FROM_SCRATCH ────────────────────────────────────
# Implements the Ken Burns + caption compositing pipeline from the handoff doc.
# User provides: pre-generated images (uploaded) + pre-generated audio (uploaded).
# Pipeline: images → Ken Burns animation → audio overlay → captions → H.264 export

def _render_synthetic_worker(job_id: str, clip: dict, image_paths: list[str], audio_path: str):
    work_dir = TEMP_DIR / job_id
    work_dir.mkdir(exist_ok=True)

    try:
        asm   = clip.get("asset_assembly_instructions", {})
        meta  = clip.get("publishing_metadata", {})
        hook  = clip.get("hook_text_overlay", "")
        title = meta.get("title", "synthetic_clip")
        tts_script = asm.get("text_to_speech_script", "")

        if not image_paths:
            raise RuntimeError("No images provided for SYNTHETIC render.")
        if not audio_path or not Path(audio_path).exists():
            raise RuntimeError("No audio file provided for SYNTHETIC render.")

        job_update(job_id, progress=5, message="Loading assets…")

        # ── Get audio duration via ffprobe ────────────────────────────────────
        probe_cmd = [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "csv=p=0",
            audio_path
        ]
        probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=30)
        if probe.returncode != 0:
            raise RuntimeError("ffprobe failed on audio file.")
        master_duration = float(probe.stdout.strip())

        # ── Word-count ratio matrix: allocate duration per scene ──────────────
        # Split script into scenes by sentence (each image = one scene)
        if tts_script:
            sentences = re.split(r'(?<=[.!?])\s+', tts_script.strip())
            sentences = [s.strip() for s in sentences if s.strip()]
        else:
            sentences = [f"Scene {i+1}" for i in range(len(image_paths))]

        n_images   = len(image_paths)
        n_scenes   = min(n_images, len(sentences))
        # Trim to matched count
        image_paths = image_paths[:n_scenes]
        sentences   = sentences[:n_scenes]

        total_words  = sum(len(s.split()) for s in sentences)
        scene_durations = [
            (len(s.split()) / max(total_words, 1)) * master_duration
            for s in sentences
        ]

        W, H = 1080, 1920
        FPS  = 30

        job_update(job_id, progress=15, message="Rendering Ken Burns frames…")

        # ── Per-scene: load image, apply Ken Burns zoom, write frame sequence ─
        frame_dir = work_dir / "frames"
        frame_dir.mkdir()
        global_frame = 0

        for scene_idx, (img_path, scene_dur, caption) in enumerate(
            zip(image_paths, scene_durations, sentences)
        ):
            n_frames = max(1, int(scene_dur * FPS))

            # Load + resize image to 1080x1920
            img = Image.open(img_path).convert("RGB")

            # Fit image into 9:16 canvas with cover (crop center)
            img_aspect = img.width / img.height
            canvas_aspect = W / H
            if img_aspect > canvas_aspect:
                # wider than canvas — fit height, crop width
                new_h = H
                new_w = int(img_aspect * H)
            else:
                new_w = W
                new_h = int(W / img_aspect)

            img = img.resize((new_w, new_h), Image.LANCZOS)
            # Center crop
            left = (new_w - W) // 2
            top  = (new_h - H) // 2
            img  = img.crop((left, top, left + W, top + H))
            img_np = np.array(img)

            for f in range(n_frames):
                t = f / max(n_frames - 1, 1)  # 0.0 → 1.0

                # Ken Burns: S(t) = 1.0 + 0.15 * (t / D_scene)
                scale = 1.0 + 0.15 * t
                new_w_s = int(W * scale)
                new_h_s = int(H * scale)

                # Resize up
                frame = cv2.resize(img_np, (new_w_s, new_h_s), interpolation=cv2.INTER_LINEAR)

                # Center crop back to W x H
                cx = (new_w_s - W) // 2
                cy = (new_h_s - H) // 2
                frame = frame[cy:cy + H, cx:cx + W]

                # Convert RGB → BGR for OpenCV
                frame_bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

                # ── Draw caption text (Z-index 1) ─────────────────────────────
                frame_pil = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
                _draw_caption(frame_pil, caption, W, H)

                # ── Draw hook banner for first 5s globally (Z-index 2) ────────
                global_time = global_frame / FPS
                if hook and global_time <= 5.0:
                    _draw_hook_banner(frame_pil, hook, W, H)

                frame_final = cv2.cvtColor(np.array(frame_pil), cv2.COLOR_RGB2BGR)
                fname = frame_dir / f"frame_{global_frame:06d}.png"
                cv2.imwrite(str(fname), frame_final)
                global_frame += 1

        job_update(job_id, progress=70, message="Encoding video with audio…")

        # ── Encode frames → video, mux with audio ─────────────────────────────
        safe_title  = re.sub(r'[^\w\-]', '_', title)[:40]
        output_name = f"{safe_title}_{job_id[:8]}.mp4"
        output_path = str(OUTPUT_DIR / output_name)

        silent_video = str(work_dir / "silent.mp4")
        
        encode_cmd = [
    "ffmpeg", "-y",
    "-framerate", str(FPS),
    "-start_number", "0",
    "-i", str(frame_dir / "frame_%06d.png"),
    "-vf", "scale=1080:1920:flags=lanczos,format=yuv420p",
    "-c:v", "libx264",
    "-preset", "ultrafast",
    "-crf", "23",
    "-movflags", "+faststart",
    silent_video
  ]

        r = subprocess.run(encode_cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"Frame encoding failed: {r.stderr[-400:]}")

        # Mux audio, trim to shortest
        mux_cmd = [
            "ffmpeg", "-y",
            "-i", silent_video,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-movflags", "+faststart",
            output_path
        ]
        r2 = subprocess.run(mux_cmd, capture_output=True, text=True, timeout=120)
        if r2.returncode != 0:
            raise RuntimeError(f"Audio mux failed: {r2.stderr[-400:]}")

        job_update(
            job_id,
            status="done",
            progress=100,
            message="Synthetic clip rendered.",
            result={
                "filename":     output_name,
                "download_url": f"/api/download/{output_name}",
                "title":        title,
                "platform":     clip.get("target_platform", "TikTok"),
                "duration":     round(master_duration, 2)
            }
        )

    except Exception as e:
        job_update(job_id, status="error", progress=0, message=str(e))
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def _draw_caption(img_pil: Image.Image, text: str, W: int, H: int):
    """Draw centered caption text at bottom third with stroke."""
    draw  = ImageDraw.Draw(img_pil)
    # Wrap text to ~30 chars per line
    words = text.split()
    lines = []
    line  = []
    for w in words:
        line.append(w)
        if len(" ".join(line)) > 28:
            lines.append(" ".join(line))
            line = []
    if line:
        lines.append(" ".join(line))

    font_size = 46
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    line_h   = font_size + 10
    total_h  = line_h * len(lines)
    y_start  = int(H * 0.72) - total_h // 2  # lower third

    for i, ln in enumerate(lines):
        bbox = draw.textbbox((0, 0), ln, font=font)
        tw   = bbox[2] - bbox[0]
        x    = (W - tw) // 2
        y    = y_start + i * line_h
        # Stroke
        for dx in [-2, 2]:
            for dy in [-2, 2]:
                draw.text((x + dx, y + dy), ln, font=font, fill=(0, 0, 0))
        draw.text((x, y), ln, font=font, fill=(255, 255, 255))


def _draw_hook_banner(img_pil: Image.Image, hook: str, W: int, H: int):
    """Draw hook text banner at y=140 (top region), semi-transparent bg."""
    draw = ImageDraw.Draw(img_pil)
    font_size = 52
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    # Background pill
    bbox    = draw.textbbox((0, 0), hook, font=font)
    tw      = bbox[2] - bbox[0]
    th      = bbox[3] - bbox[1]
    pad     = 18
    x0      = (W - tw) // 2 - pad
    y0      = 130 - pad
    x1      = (W + tw) // 2 + pad
    y1      = 130 + th + pad

    overlay = Image.new("RGBA", img_pil.size, (0, 0, 0, 0))
    od      = ImageDraw.Draw(overlay)
    od.rounded_rectangle([x0, y0, x1, y1], radius=14, fill=(0, 0, 0, 170))
    img_pil.paste(Image.alpha_composite(img_pil.convert("RGBA"), overlay).convert("RGB"),
                  (0, 0))

    draw = ImageDraw.Draw(img_pil)
    x    = (W - tw) // 2
    draw.text((x, 130), hook, font=font, fill=(255, 220, 50))


# ─── Route: Upload asset (image or audio for synthetic) ───────────────────────

@app.route("/api/upload", methods=["POST"])
def upload_asset():
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f    = request.files["file"]
    ext  = Path(f.filename).suffix.lower()
    name = f"{uuid.uuid4().hex}{ext}"
    dest = UPLOAD_DIR / name
    f.save(str(dest))
    return jsonify({"path": str(dest), "filename": name})


# ─── Route: Download rendered clip ────────────────────────────────────────────

@app.route("/api/download/<filename>")
def download(filename: str):
    safe = Path(filename).name  # prevent traversal
    path = OUTPUT_DIR / safe
    if not path.exists():
        return jsonify({"error": "File not found"}), 404
    return send_file(str(path), as_attachment=True, download_name=safe)


# ─── Route: List rendered clips ───────────────────────────────────────────────

@app.route("/api/clips")
def list_clips():
    clips = []
    for f in sorted(OUTPUT_DIR.glob("*.mp4"), key=os.path.getmtime, reverse=True):
        clips.append({
            "filename":     f.name,
            "download_url": f"/api/download/{f.name}",
            "size_mb":      round(f.stat().st_size / 1024 / 1024, 2),
            "created":      datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        })
    return jsonify(clips)



