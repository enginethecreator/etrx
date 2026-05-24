import os
import re
import json
import uuid
import threading
import subprocess
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# STARTUP — directories + font resolution
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
for d in ["uploads", "outputs", "temp"]:
    (BASE_DIR / d).mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

KNOWN_FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf",
]

def find_font(size: int = 48):
    for path in KNOWN_FONT_PATHS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()

FONT_CAPTION = find_font(44)
FONT_HOOK    = find_font(56)
FONT_PATH_RESOLVED = next((p for p in KNOWN_FONT_PATHS if os.path.exists(p)), "")

app = FastAPI(title="ClipForge")

# ---------------------------------------------------------------------------
# IN-MEMORY JOB STORE
# ---------------------------------------------------------------------------
JOBS: dict = {}

def job_set(job_id: str, state: str, data: dict = None, error: str = None):
    JOBS[job_id] = {"state": state, "data": data or {}, "error": error}

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

def parse_vtt(vtt_text: str) -> list:
    segs = []
    time_re = re.compile(r"(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})")
    tag_re  = re.compile(r"<[^>]+>")
    lines = vtt_text.splitlines()
    i = 0
    while i < len(lines):
        m = time_re.search(lines[i])
        if m:
            def ts_to_sec(ts):
                h, mn, s = ts.split(":")
                return int(h)*3600 + int(mn)*60 + float(s)
            s_start = ts_to_sec(m.group(1))
            s_end   = ts_to_sec(m.group(2))
            i += 1
            text_parts = []
            while i < len(lines) and lines[i].strip() and not time_re.search(lines[i]):
                clean = tag_re.sub("", lines[i]).strip()
                if clean:
                    text_parts.append(clean)
                i += 1
            if text_parts:
                segs.append({
                    "start": s_start,
                    "duration": s_end - s_start,
                    "text": " ".join(text_parts)
                })
        else:
            i += 1
    return segs

def parse_json3(j3: dict) -> list:
    segs = []
    for event in j3.get("events", []):
        if "segs" not in event:
            continue
        start = event.get("tStartMs", 0) / 1000.0
        dur   = event.get("dDurationMs", 0) / 1000.0
        text  = "".join(s.get("utf8", "") for s in event["segs"]).replace("\n", " ").strip()
        if text:
            segs.append({"start": start, "duration": dur, "text": text})
    return segs

def merge_to_sentences(segments: list) -> list:
    merged = []
    current = None
    for seg in segments:
        s = seg["start"]
        e = round(seg["start"] + seg["duration"], 3)
        t = seg["text"].strip()
        if not t:
            continue
        if current is None:
            current = {"start": s, "end": e, "text": t}
            continue
        overlap       = s < current["end"]
        chunk_dur     = current["end"] - current["start"]
        last_char     = current["text"].rstrip()[-1] if current["text"].rstrip() else ""
        ends_sentence = last_char in SENTENCE_ENDERS
        if ends_sentence and not overlap and chunk_dur >= 3.0:
            merged.append(current)
            current = {"start": s, "end": e, "text": t}
        else:
            current["end"]  = max(current["end"], e)
            current["text"] += " " + t
    if current:
        merged.append(current)
    return [
        {"start": seconds_to_ts(c["start"]), "end": seconds_to_ts(c["end"]), "text": c["text"]}
        for c in merged
    ]

# ---------------------------------------------------------------------------
# EXTRACT WORKER
# ---------------------------------------------------------------------------
def extract_worker(job_id: str, url: str):
    try:
        meta_cmd = ["yt-dlp", "--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(f"yt-dlp metadata failed: {meta_res.stderr[:300]}")
        meta     = json.loads(meta_res.stdout.strip().splitlines()[-1])
        video_id = meta.get("id", "unknown")
        title    = meta.get("title", "")
        duration = meta.get("duration_string", "")
        thumb    = meta.get("thumbnail", "")

        sub_base = str(BASE_DIR / "temp" / f"sub_{job_id}")
        segments = []

        # json3 first
        j3_cmd = [
            "yt-dlp", "--write-auto-subs", "--sub-langs", "en",
            "--sub-format", "json3", "--skip-download",
            "--output", sub_base, url
        ]
        subprocess.run(j3_cmd, capture_output=True, timeout=60)
        j3_file = f"{sub_base}.en.json3"
        if os.path.exists(j3_file):
            with open(j3_file, "r", encoding="utf-8") as f:
                segments = parse_json3(json.load(f))
            os.remove(j3_file)

        # VTT fallback
        if not segments:
            vtt_cmd = [
                "yt-dlp", "--write-auto-subs", "--sub-langs", "en",
                "--sub-format", "vtt", "--skip-download",
                "--output", sub_base, url
            ]
            subprocess.run(vtt_cmd, capture_output=True, timeout=60)
            vtt_file = f"{sub_base}.en.vtt"
            if os.path.exists(vtt_file):
                with open(vtt_file, "r", encoding="utf-8") as f:
                    segments = parse_vtt(f.read())
                os.remove(vtt_file)

        if not segments:
            raise RuntimeError("No subtitles found — json3 and VTT both failed")

        full_text  = " ".join(s["text"] for s in segments)
        transcript = merge_to_sentences(segments)

        job_set(job_id, "done", {
            "video_id": video_id,
            "title": title,
            "duration": duration,
            "thumbnail": thumb,
            "full_text": full_text,
            "segments": segments,
            "transcript": transcript,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

# ---------------------------------------------------------------------------
# RENDER — SLICED_FROM_SOURCE WORKER
# ---------------------------------------------------------------------------
def render_sliced_worker(job_id: str, url: str, blueprint: dict):
    raw_path = str(BASE_DIR / "temp" / f"raw_{job_id}.mp4")
    out_path = str(BASE_DIR / "outputs" / f"{job_id}.mp4")
    try:
        dl_cmd = [
            "yt-dlp", "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
            "--output", raw_path, "--no-playlist", url
        ]
        subprocess.run(dl_cmd, check=True, capture_output=True, timeout=600)

        ts_start     = blueprint["timestamp_start"]
        ts_end       = blueprint["timestamp_end"]
        hook         = blueprint.get("hook_text_overlay", "")[:50]
        hook_escaped = hook.replace("'", "\\'").replace(":", "\\:").replace("\\", "\\\\")

        drawtext = (
            f"drawtext=text='{hook_escaped}'"
            f":fontsize=54:fontcolor=white:borderw=3:bordercolor=black"
            f":x=(w-text_w)/2:y=h*0.12"
            f":enable='lt(t\\,5)'"
        )
        if FONT_PATH_RESOLVED:
            drawtext += f":fontfile='{FONT_PATH_RESOLVED}'"

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", raw_path,
            "-ss", ts_start, "-to", ts_end,
            "-vf", f"crop=ih*9/16:ih,scale=1080:1920,{drawtext}",
            "-c:v", "libx264", "-profile:v", "main", "-level:v", "4.0",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            out_path
        ]
        subprocess.run(ffmpeg_cmd, check=True, capture_output=True, timeout=300)
        job_set(job_id, "done", {"output": f"{job_id}.mp4", "type": "sliced"})
    except subprocess.CalledProcessError as ex:
        job_set(job_id, "error", error=(ex.stderr.decode()[-400:] if ex.stderr else str(ex)))
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)

# ---------------------------------------------------------------------------
# RENDER — SYNTHETIC_FROM_SCRATCH WORKER
# ---------------------------------------------------------------------------
W, H, FPS = 1080, 1920, 30

def wrap_text_pixels(text: str, font, max_px: int) -> list:
    dummy = Image.new("RGB", (1, 1))
    draw  = ImageDraw.Draw(dummy)
    words = text.split()
    lines, current = [], ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textlength(test, font=font) <= max_px:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines

def get_audio_duration(path: str) -> float:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path]
    res = subprocess.run(cmd, capture_output=True, text=True)
    try:
        data = json.loads(res.stdout)
        for stream in data.get("streams", []):
            dur = stream.get("duration")
            if dur:
                return float(dur)
    except Exception:
        pass
    return 0.0

def render_synthetic_worker(job_id: str, image_paths: list, audio_path: str, blueprint: dict):
    silent_path = str(BASE_DIR / "temp" / f"silent_{job_id}.mp4")
    out_path    = str(BASE_DIR / "outputs" / f"{job_id}.mp4")
    try:
        hook     = blueprint.get("hook_text_overlay", "")[:50]
        script   = blueprint.get("asset_assembly_instructions", {}).get("text_to_speech_script", "")
        D_master = get_audio_duration(audio_path)
        if D_master <= 0:
            raise RuntimeError("ffprobe could not determine audio duration")

        n_scenes = len(image_paths)
        words    = script.split()
        W_total  = max(len(words), 1)

        # Proportional word count per scene
        base = W_total // n_scenes
        rem  = W_total % n_scenes
        scene_word_counts = [base + (1 if i < rem else 0) for i in range(n_scenes)]

        # Open FFmpeg pipe
        pipe_cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
            "-r", str(FPS), "-i", "-",
            "-an",
            "-c:v", "libx264", "-profile:v", "main", "-level:v", "4.0",
            "-movflags", "+faststart",
            silent_path
        ]
        proc = subprocess.Popen(
            pipe_cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )

        global_frame = 0
        word_idx     = 0

        for scene_i, img_path in enumerate(image_paths):
            w_scene  = scene_word_counts[scene_i]
            D_scene  = (w_scene / W_total) * D_master
            N_frames = max(int(D_scene * FPS), 1)

            img_bgr = cv2.imread(img_path)
            if img_bgr is None:
                img_bgr = np.zeros((H, W, 3), dtype=np.uint8)
            img_bgr = cv2.resize(img_bgr, (W, H), interpolation=cv2.INTER_LANCZOS4)

            scene_words  = words[word_idx: word_idx + w_scene]
            word_idx    += w_scene
            caption_text = " ".join(scene_words)

            for f in range(N_frames):
                # Ken Burns
                scale  = 1.0 + (0.15 * f / max(N_frames - 1, 1))
                new_w  = int(W * scale)
                new_h  = int(H * scale)
                scaled = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                cx     = (new_w - W) // 2
                cy     = (new_h - H) // 2
                frame_bgr = scaled[cy:cy+H, cx:cx+W].copy()

                frame_pil = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
                draw      = ImageDraw.Draw(frame_pil)

                # Z1: captions at y=0.75*H — yellow, 8pt stroke
                cap_lines  = wrap_text_pixels(caption_text, FONT_CAPTION, W - 80)
                cap_line_h = FONT_CAPTION.size + 8
                cap_y      = int(H * 0.75)
                for line in cap_lines:
                    tw = draw.textlength(line, font=FONT_CAPTION)
                    tx = (W - tw) / 2
                    for dx in range(-4, 5, 4):
                        for dy in range(-4, 5, 4):
                            if dx != 0 or dy != 0:
                                draw.text((tx+dx, cap_y+dy), line, font=FONT_CAPTION, fill=(0,0,0))
                    draw.text((tx, cap_y), line, font=FONT_CAPTION, fill=(255, 255, 0))
                    cap_y += cap_line_h

                # Z2: hook banner — white, first 5s globally
                global_t = global_frame / FPS
                if global_t < 5.0:
                    hook_lines  = wrap_text_pixels(hook, FONT_HOOK, W - 80)
                    hook_line_h = FONT_HOOK.size + 10
                    hook_y      = int(H * 0.12)
                    for line in hook_lines:
                        tw = draw.textlength(line, font=FONT_HOOK)
                        tx = (W - tw) / 2
                        for dx in range(-4, 5, 4):
                            for dy in range(-4, 5, 4):
                                if dx != 0 or dy != 0:
                                    draw.text((tx+dx, hook_y+dy), line, font=FONT_HOOK, fill=(0,0,0))
                        draw.text((tx, hook_y), line, font=FONT_HOOK, fill=(255, 255, 255))
                        hook_y += hook_line_h

                frame_out = cv2.cvtColor(np.array(frame_pil), cv2.COLOR_RGB2BGR)
                proc.stdin.write(frame_out.tobytes())
                global_frame += 1

        proc.stdin.close()
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg pipe error: {proc.stderr.read().decode()[-300:]}")

        # Mux with audio
        mux_cmd = [
            "ffmpeg", "-y",
            "-i", silent_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac",
            "-shortest", "-movflags", "+faststart",
            out_path
        ]
        subprocess.run(mux_cmd, check=True, capture_output=True, timeout=120)
        job_set(job_id, "done", {"output": f"{job_id}.mp4", "type": "synthetic"})

    except Exception as ex:
        job_set(job_id, "error", error=str(ex))
    finally:
        if os.path.exists(silent_path):
            os.remove(silent_path)
        for p in image_paths:
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(audio_path):
            os.remove(audio_path)

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
class ExtractRequest(BaseModel):
    url: str

class SlicedRenderRequest(BaseModel):
    url: str
    blueprint: dict

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/extract")
async def api_extract(req: ExtractRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=extract_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/render/sliced")
async def api_render_sliced(req: SlicedRenderRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(
        target=render_sliced_worker,
        args=(job_id, req.url, req.blueprint), daemon=True
    ).start()
    return {"job_id": job_id}

@app.post("/api/render/synthetic")
async def api_render_synthetic(
    blueprint: str = Form(...),
    images: list[UploadFile] = File(...),
    audio: UploadFile = File(...),
):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")

    image_paths = []
    for i, img in enumerate(images):
        ext  = Path(img.filename).suffix or ".jpg"
        path = str(BASE_DIR / "temp" / f"{job_id}_img{i}{ext}")
        with open(path, "wb") as f:
            f.write(await img.read())
        image_paths.append(path)

    audio_ext  = Path(audio.filename).suffix or ".mp3"
    audio_path = str(BASE_DIR / "temp" / f"{job_id}_audio{audio_ext}")
    with open(audio_path, "wb") as f:
        f.write(await audio.read())

    bp = json.loads(blueprint)
    threading.Thread(
        target=render_synthetic_worker,
        args=(job_id, image_paths, audio_path, bp), daemon=True
    ).start()
    return {"job_id": job_id}

@app.get("/api/job/{job_id}")
async def api_job_status(job_id: str):
    return JOBS.get(job_id, {"state": "not_found"})

@app.get("/api/clips")
async def api_list_clips():
    clips = []
    for f in sorted((BASE_DIR / "outputs").glob("*.mp4")):
        clips.append({"filename": f.name, "size_mb": round(f.stat().st_size / 1e6, 2)})
    return clips

@app.get("/api/download/{filename}")
async def api_download(filename: str):
    path = BASE_DIR / "outputs" / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(path), media_type="video/mp4", filename=filename)
