import os
import re
import json
import uuid
import threading
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from fastapi import Request
from pydantic import BaseModel
import uvicorn

# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
app = FastAPI(title="ClipForge")

for folder in ["uploads", "outputs", "temp", "templates"]:
    Path(folder).mkdir(exist_ok=True)

templates = Jinja2Templates(directory="templates")

FPS = 30
W, H = 1080, 1920

# ---------------------------------------------------------------------------
# FONT RESOLUTION — never hardcoded
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]

RESOLVED_FONT_PATH: Optional[str] = None
for _fp in _FONT_CANDIDATES:
    if os.path.exists(_fp):
        RESOLVED_FONT_PATH = _fp
        break


def find_font(size: int = 48) -> ImageFont.FreeTypeFont:
    if RESOLVED_FONT_PATH:
        try:
            return ImageFont.truetype(RESOLVED_FONT_PATH, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# IN-MEMORY JOB STORE
# ---------------------------------------------------------------------------
JOBS: Dict[str, Dict[str, Any]] = {}


def new_job() -> str:
    job_id = str(uuid.uuid4())
    JOBS[job_id] = {"state": "running", "result": None, "error": None}
    return job_id


def job_done(job_id: str, result: Any):
    JOBS[job_id]["state"] = "done"
    JOBS[job_id]["result"] = result


def job_error(job_id: str, msg: str):
    JOBS[job_id]["state"] = "error"
    JOBS[job_id]["error"] = msg


# ---------------------------------------------------------------------------
# SUBTITLE UTILS
# ---------------------------------------------------------------------------
SENTENCE_ENDERS = {'.', '?', '!', '"', '\u201d'}


def seconds_to_ts(s: float) -> str:
    s = round(s, 3)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"


def ts_to_seconds(ts: str) -> float:
    parts = ts.split(":")
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s


def parse_json3(data: dict) -> list:
    segs = []
    for event in data.get("events", []):
        start_ms = event.get("tStartMs", 0)
        dur_ms = event.get("dDurationMs", 0)
        text = "".join(s.get("utf8", "") for s in event.get("segs", [])).strip()
        if text and text != "\n":
            segs.append({"start": start_ms / 1000.0, "duration": dur_ms / 1000.0, "text": text})
    return segs


def parse_vtt(content: str) -> list:
    segs = []
    time_re = re.compile(r"(\d{2}:\d{2}:\d{2}\.\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}\.\d{3})")
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        m = time_re.search(lines[i])
        if m:
            start = ts_to_seconds(m.group(1))
            end = ts_to_seconds(m.group(2))
            i += 1
            text_parts = []
            while i < len(lines) and lines[i].strip() and not time_re.search(lines[i]):
                clean = re.sub(r"<[^>]+>", "", lines[i]).strip()
                if clean:
                    text_parts.append(clean)
                i += 1
            text = " ".join(text_parts).strip()
            if text:
                segs.append({"start": start, "duration": end - start, "text": text})
        else:
            i += 1
    return segs


def merge_segments(raw_segs: list) -> list:
    merged = []
    current = None
    for i, seg in enumerate(raw_segs):
        s_start = seg["start"]
        s_end = round(seg["start"] + seg["duration"], 3)
        s_text = seg["text"].strip()
        if not s_text:
            continue
        if current is None:
            current = {"start": s_start, "end": s_end, "text": s_text}
            continue
        overlap = s_start < current["end"]
        last_char = current["text"].rstrip()[-1] if current["text"].rstrip() else ""
        ends_sentence = last_char in SENTENCE_ENDERS
        chunk_duration = current["end"] - current["start"]
        next_start = raw_segs[i + 1]["start"] if i + 1 < len(raw_segs) else current["end"] + 99
        no_overlap_next = next_start >= current["end"]
        if ends_sentence and chunk_duration >= 3.0 and no_overlap_next and not overlap:
            merged.append({
                "start": seconds_to_ts(current["start"]),
                "end": seconds_to_ts(current["end"]),
                "text": current["text"],
            })
            current = {"start": s_start, "end": s_end, "text": s_text}
        else:
            current["end"] = max(current["end"], s_end)
            current["text"] += " " + s_text
    if current:
        merged.append({
            "start": seconds_to_ts(current["start"]),
            "end": seconds_to_ts(current["end"]),
            "text": current["text"],
        })
    return merged


# ---------------------------------------------------------------------------
# EXTRACT WORKER
# ---------------------------------------------------------------------------
def extract_worker(job_id: str, url: str):
    try:
        tmp_dir = Path(f"temp/extract_{job_id}")
        tmp_dir.mkdir(parents=True, exist_ok=True)

        meta_result = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, check=True
        )
        meta = json.loads(meta_result.stdout.strip().splitlines()[0])

        video_id = meta.get("id", "unknown")
        title = meta.get("title", "Unknown")
        duration = meta.get("duration_string", "?")
        thumbnail = meta.get("thumbnail", "")
        uploader = meta.get("uploader", "Unknown")

        sub_base = str(tmp_dir / f"sub_{video_id}")
        raw_segs = []

        # json3 first
        subprocess.run([
            "yt-dlp", "--write-auto-subs", "--sub-langs", "en",
            "--sub-format", "json3", "--skip-download",
            "--output", sub_base, url,
        ], capture_output=True)

        j3_file = Path(f"{sub_base}.en.json3")
        if j3_file.exists():
            with open(j3_file, "r", encoding="utf-8") as f:
                raw_segs = parse_json3(json.load(f))
            j3_file.unlink(missing_ok=True)
        else:
            # VTT fallback
            subprocess.run([
                "yt-dlp", "--write-auto-subs", "--sub-langs", "en",
                "--sub-format", "vtt", "--skip-download",
                "--output", sub_base, url,
            ], capture_output=True)
            vtt_file = Path(f"{sub_base}.en.vtt")
            if vtt_file.exists():
                with open(vtt_file, "r", encoding="utf-8") as f:
                    raw_segs = parse_vtt(f.read())
                vtt_file.unlink(missing_ok=True)

        for f in tmp_dir.iterdir():
            f.unlink(missing_ok=True)
        tmp_dir.rmdir()

        full_text = " ".join(s["text"] for s in raw_segs)
        transcript = merge_segments(raw_segs)

        job_done(job_id, {
            "video_id": video_id,
            "title": title,
            "duration": duration,
            "thumbnail": thumbnail,
            "uploader": uploader,
            "full_text": full_text,
            "segments": raw_segs,
            "transcript": transcript,
        })
    except Exception as e:
        job_error(job_id, str(e))


# ---------------------------------------------------------------------------
# RENDER — SLICED_FROM_SOURCE WORKER
# ---------------------------------------------------------------------------
def render_sliced_worker(job_id: str, url: str, blueprint: dict):
    try:
        ts_start = blueprint.get("timestamp_start", "00:00:00.000")
        ts_end = blueprint.get("timestamp_end", "00:00:30.000")
        hook_text = blueprint.get("hook_text_overlay", "")

        raw_path = Path(f"temp/raw_{job_id}.mp4")
        output_path = Path(f"outputs/clip_{job_id}.mp4")

        subprocess.run([
            "yt-dlp",
            "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
            "--output", str(raw_path),
            url,
        ], check=True, capture_output=True)

        safe_hook = hook_text.replace("'", "\\'").replace(":", "\\:").replace("\\", "\\\\")
        font_arg = f":fontfile={RESOLVED_FONT_PATH}" if RESOLVED_FONT_PATH else ""
        drawtext = (
            f"drawtext=text='{safe_hook}'"
            f"{font_arg}"
            f":fontsize=52:fontcolor=white:borderw=4:bordercolor=black"
            f":x=(w-text_w)/2:y=h*0.12:enable='lte(t\\,5)'"
        )

        subprocess.run([
            "ffmpeg", "-y",
            "-i", str(raw_path),
            "-ss", ts_start,
            "-to", ts_end,
            "-vf", f"crop=ih*9/16:ih,scale=1080:1920,{drawtext}",
            "-c:v", "libx264", "-profile:v", "main", "-level:v", "4.0",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(output_path),
        ], check=True, capture_output=True)

        raw_path.unlink(missing_ok=True)
        job_done(job_id, {"output_file": output_path.name, "type": "sliced"})
    except Exception as e:
        job_error(job_id, str(e))


# ---------------------------------------------------------------------------
# TEXT WRAPPING — pixel-boundary only
# ---------------------------------------------------------------------------
def wrap_text_pixel(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list:
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textlength(test, font=font) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# RENDER — SYNTHETIC_FROM_SCRATCH WORKER
# ---------------------------------------------------------------------------
def render_synthetic_worker(job_id: str, image_paths: list, audio_path: str, blueprint: dict):
    try:
        hook_text = blueprint.get("hook_text_overlay", "")
        script = blueprint.get("asset_assembly_instructions", {}).get("text_to_speech_script", "") or ""
        output_path = Path(f"outputs/clip_{job_id}.mp4")
        silent_path = Path(f"temp/silent_{job_id}.mp4")

        # Audio duration via ffprobe
        probe = subprocess.run([
            "ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio_path,
        ], capture_output=True, text=True, check=True)
        D_master = float(json.loads(probe.stdout)["format"]["duration"])

        N_scenes = len(image_paths)
        all_words = script.split()
        W_total = max(len(all_words), N_scenes)

        scene_configs = []
        for i, img_path in enumerate(image_paths):
            w_start = int(i * W_total / N_scenes)
            w_end = int((i + 1) * W_total / N_scenes) if i < N_scenes - 1 else W_total
            w_scene = max(1, w_end - w_start)
            D_scene = (w_scene / W_total) * D_master
            F_scene = max(1, int(D_scene * FPS))
            scene_configs.append({
                "img_path": img_path,
                "frames": F_scene,
                "word_start": w_start,
                "word_end": w_end,
                "duration": D_scene,
            })

        font_caption = find_font(42)
        font_hook = find_font(58)

        total_frames = sum(s["frames"] for s in scene_configs)

        # Open FFmpeg stdin pipe — zero disk I/O for frames
        pipe = subprocess.Popen([
            "ffmpeg", "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24",
            "-s", f"{W}x{H}",
            "-r", str(FPS),
            "-i", "-",
            "-c:v", "libx264",
            "-profile:v", "main", "-level:v", "4.0",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(silent_path),
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        global_frame = 0

        for scene in scene_configs:
            img = Image.open(scene["img_path"]).convert("RGB")
            iw, ih = img.size
            target_ar = W / H
            src_ar = iw / ih
            if src_ar > target_ar:
                cw = int(ih * target_ar)
                ch = ih
            else:
                cw = iw
                ch = int(iw / target_ar)
            cx, cy = iw // 2, ih // 2
            img_cropped = img.crop((cx - cw // 2, cy - ch // 2, cx + cw // 2, cy + ch // 2))
            img_base = np.array(img_cropped.resize((W, H), Image.LANCZOS))

            N_frames = scene["frames"]
            scene_words = all_words[scene["word_start"]:scene["word_end"]]
            words_per_frame = len(scene_words) / max(N_frames, 1)

            for f in range(N_frames):
                # Ken Burns zoom: S(t) = 1.0 + 0.15 * f/(N-1)
                scale = 1.0 + (0.15 * f / max(N_frames - 1, 1))
                nw, nh = int(W * scale), int(H * scale)
                zoomed = cv2.resize(img_base, (nw, nh), interpolation=cv2.INTER_LINEAR)
                ox, oy = (nw - W) // 2, (nh - H) // 2
                frame_bgr = zoomed[oy:oy + H, ox:ox + W]

                frame_pil = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
                draw = ImageDraw.Draw(frame_pil)

                # Z2: hook banner — first 5 seconds globally
                if (global_frame / FPS) <= 5.0:
                    hook_lines = wrap_text_pixel(draw, hook_text, font_hook, int(W * 0.85))
                    hy = int(H * 0.12)
                    for line in hook_lines:
                        bb = draw.textbbox((0, 0), line, font=font_hook)
                        lw = bb[2] - bb[0]
                        lx = (W - lw) // 2
                        draw.text((lx, hy), line, font=font_hook, fill="white",
                                  stroke_width=5, stroke_fill="black")
                        hy += bb[3] - bb[1] + 6

                # Z1: caption at y = 0.75*H
                wi = int(f * words_per_frame)
                caption = " ".join(scene_words[wi:wi + 8])
                if caption:
                    cap_lines = wrap_text_pixel(draw, caption, font_caption, int(W * 0.85))
                    cy_pos = int(H * 0.75)
                    for line in cap_lines:
                        bb = draw.textbbox((0, 0), line, font=font_caption)
                        lw = bb[2] - bb[0]
                        lx = (W - lw) // 2
                        draw.text((lx, cy_pos), line, font=font_caption, fill="yellow",
                                  stroke_width=8, stroke_fill="black")
                        cy_pos += bb[3] - bb[1] + 4

                out_bgr = cv2.cvtColor(np.array(frame_pil), cv2.COLOR_RGB2BGR)
                pipe.stdin.write(out_bgr.tobytes())
                global_frame += 1

        pipe.stdin.close()
        pipe.wait()

        # Mux silent video + audio
        subprocess.run([
            "ffmpeg", "-y",
            "-i", str(silent_path),
            "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac",
            "-shortest", "-movflags", "+faststart",
            str(output_path),
        ], check=True, capture_output=True)

        silent_path.unlink(missing_ok=True)
        for ip in image_paths:
            Path(ip).unlink(missing_ok=True)
        Path(audio_path).unlink(missing_ok=True)

        job_done(job_id, {"output_file": output_path.name, "type": "synthetic"})
    except Exception as e:
        job_error(job_id, str(e))


# ---------------------------------------------------------------------------
# API ROUTES
# ---------------------------------------------------------------------------
class ExtractRequest(BaseModel):
    url: str


class RenderSlicedRequest(BaseModel):
    url: str
    blueprint: dict


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/extract")
async def extract(req: ExtractRequest):
    job_id = new_job()
    threading.Thread(target=extract_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/render/sliced")
async def render_sliced(req: RenderSlicedRequest):
    job_id = new_job()
    threading.Thread(target=render_sliced_worker, args=(job_id, req.url, req.blueprint), daemon=True).start()
    return {"job_id": job_id}


@app.post("/api/render/synthetic")
async def render_synthetic(
    blueprint: str = Form(...),
    audio: UploadFile = File(...),
    images: list[UploadFile] = File(...),
):
    job_id = new_job()
    bp = json.loads(blueprint)

    audio_path = f"temp/audio_{job_id}{Path(audio.filename).suffix}"
    with open(audio_path, "wb") as f:
        f.write(await audio.read())

    image_paths = []
    for i, img_file in enumerate(images):
        ext = Path(img_file.filename).suffix or ".jpg"
        img_path = f"temp/img_{job_id}_{i:03d}{ext}"
        with open(img_path, "wb") as f:
            f.write(await img_file.read())
        image_paths.append(img_path)
    image_paths.sort()

    threading.Thread(
        target=render_synthetic_worker,
        args=(job_id, image_paths, audio_path, bp),
        daemon=True,
    ).start()
    return {"job_id": job_id}


@app.get("/api/job/{job_id}")
async def get_job(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]


@app.get("/api/clips")
async def list_clips():
    clips = []
    for f in sorted(Path("outputs").iterdir()):
        if f.suffix == ".mp4":
            clips.append({"name": f.name, "size_mb": round(f.stat().st_size / 1e6, 2)})
    return clips


@app.get("/api/clips/{filename}")
async def download_clip(filename: str):
    path = Path("outputs") / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(path), media_type="video/mp4", filename=filename)


if __name__ == "__main__":
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False, workers=1)
