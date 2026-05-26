import os
import re
import json
import uuid
import subprocess
import threading
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel, validator

# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
BASE_DIR  = Path(__file__).parent
COOKIES   = BASE_DIR / "cookies.txt"
DOWNLOADS = BASE_DIR / "downloads"
CLIPS     = BASE_DIR / "clips"
TEMP      = BASE_DIR / "temp"

DOWNLOADS.mkdir(exist_ok=True)
CLIPS.mkdir(exist_ok=True)
TEMP.mkdir(exist_ok=True)

app = FastAPI(title="ClipForge")

# ---------------------------------------------------------------------------
# JOB STORE
# ---------------------------------------------------------------------------
JOBS: dict = {}

def job_set(job_id: str, state: str, data: dict = None, error: str = None):
    JOBS[job_id] = {"state": state, "data": data or {}, "error": error}

# ---------------------------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------------------------
def seconds_to_ts(s: float) -> str:
    s = round(s, 3)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"

def ts_to_seconds(ts: str) -> float:
    parts = ts.split(':')
    if len(parts) != 3:
        raise ValueError("Timestamp must be HH:MM:SS[.mmm]")
    h, m = int(parts[0]), int(parts[1])
    sec_part = parts[2]
    seconds = float(sec_part[0:2]) + (float('0.' + sec_part.split('.')[1]) if '.' in sec_part else 0)
    return h * 3600 + m * 60 + seconds

def _timestamp_to_seconds(ts: str) -> float:
    parts = ts.split(":")
    if len(parts) != 3:
        raise ValueError("Timestamp must be HH:MM:SS")
    return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])

def validate_timestamp(ts: str) -> bool:
    pattern = r'^\d{1,2}:\d{1,2}:\d{2}(?:\.\d{1,3})?$'
    if not re.match(pattern, ts):
        return False
    try:
        _timestamp_to_seconds(ts)
        return True
    except:
        return False

def ytdlp_base() -> list:
    cmd = ["yt-dlp"]
    if COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    return cmd

def extract_plain_text_from_vtt(vtt_path: Path) -> str:
    if not vtt_path.exists():
        return ""
    lines = []
    with open(vtt_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not ('-->' in line or line.startswith('WEBVTT') or line.isdigit()):
                clean = re.sub(r'<[^>]+>', '', line)
                if clean:
                    lines.append(clean)
    return ' '.join(lines)

def get_file_size_mb(path: Path) -> float:
    return round(path.stat().st_size / (1024 * 1024), 2) if path.exists() else 0.0

def find_existing_download(video_id: str):
    """Return first mp4 in DOWNLOADS matching video_id prefix."""
    for f in DOWNLOADS.glob(f"{video_id}_*.mp4"):
        return f
    return None

# ---------------------------------------------------------------------------
# WORKERS
# ---------------------------------------------------------------------------
def check_worker(job_id: str, url: str):
    """Check metadata + whether file already exists, without downloading."""
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])

        meta       = json.loads(meta_res.stdout.strip().splitlines()[-1])
        video_id   = meta.get("id", "unknown")
        title      = meta.get("title", "untitled")
        duration_raw = meta.get("duration", 0)
        try:
            duration_sec = float(duration_raw)
        except (TypeError, ValueError):
            duration_sec = 0
        duration_str = seconds_to_ts(duration_sec) if duration_sec > 0 else "00:00:00.000"
        thumbnail    = meta.get("thumbnail", "")

        existing = find_existing_download(video_id)
        if existing:
            job_set(job_id, "done", {
                "exists": True,
                "video_id": video_id,
                "title": title,
                "duration_sec": duration_sec,
                "duration_str": duration_str,
                "thumbnail": thumbnail,
                "filename": existing.name,
                "size_mb": get_file_size_mb(existing),
            })
        else:
            job_set(job_id, "done", {
                "exists": False,
                "video_id": video_id,
                "title": title,
                "duration_sec": duration_sec,
                "duration_str": duration_str,
                "thumbnail": thumbnail,
                "filename": None,
                "size_mb": 0,
            })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))


def download_worker(job_id: str, url: str, video_id: str, title: str,
                    duration_sec: float, duration_str: str):
    """Full download — called only after check confirms file doesn't exist."""
    try:
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:60]
        out_file   = DOWNLOADS / f"{video_id}_{safe_title}.mp4"

        # Re-check in case it appeared between check and download
        existing = find_existing_download(video_id)
        if existing:
            job_set(job_id, "done", {
                "video_id": video_id,
                "title": title,
                "duration_sec": duration_sec,
                "duration_str": duration_str,
                "filename": existing.name,
                "size_mb": get_file_size_mb(existing),
                "subtitle_text": "",
            })
            return

        dl_cmd = ytdlp_base() + [
            "-S", "vcodec:h264,res,acodec:aac",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", str(out_file),
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-400:])

        # Subtitles
        vtt_path = out_file.with_suffix('.en.vtt')
        sub_cmd  = ytdlp_base() + [
            "--skip-download", "--write-auto-subs", "--write-subs",
            "--sub-langs", "en", "--sub-format", "vtt",
            "-o", str(out_file.with_suffix('')),
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)

        job_set(job_id, "done", {
            "video_id": video_id,
            "title": title,
            "duration_sec": duration_sec,
            "duration_str": duration_str,
            "filename": out_file.name,
            "size_mb": get_file_size_mb(out_file),
            "subtitle_text": sub_text,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))


def subtitle_only_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])
        meta  = json.loads(meta_res.stdout.strip().splitlines()[-1])
        title = meta.get("title", "untitled")

        tmp_base = TEMP / f"sub_{job_id}"
        sub_cmd  = ytdlp_base() + [
            "--skip-download", "--write-auto-subs", "--write-subs",
            "--sub-langs", "en", "--sub-format", "vtt",
            "-o", str(tmp_base), url
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        vtt_path = Path(str(tmp_base) + ".en.vtt")
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)

        job_set(job_id, "done", {"title": title, "subtitle_text": sub_text})
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))


def subtitle_segments_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])
        meta  = json.loads(meta_res.stdout.strip().splitlines()[-1])
        title = meta.get("title", "untitled")

        tmp_base = TEMP / f"segments_{job_id}"
        sub_cmd  = ytdlp_base() + [
            "--skip-download", "--write-auto-subs", "--write-subs",
            "--sub-langs", "en", "--sub-format", "json3",
            "-o", str(tmp_base), url
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)

        json3_path = Path(str(tmp_base) + ".en.json3")
        if not json3_path.exists():
            raise RuntimeError("No JSON3 subtitles found (English).")

        with open(json3_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        segments = []
        for event in data.get("events", []):
            start_ms    = event.get("tStartMs", 0)
            duration_ms = event.get("dDurationMs", 0)
            text        = "".join(s.get("utf8", "") for s in event.get("segs", [])).strip()
            if not text or text == "\n":
                continue
            segments.append({
                "start": seconds_to_ts(start_ms / 1000.0),
                "end":   seconds_to_ts((start_ms + duration_ms) / 1000.0),
                "text":  text
            })

        json3_path.unlink(missing_ok=True)
        job_set(job_id, "done", {"segments": segments, "title": title})
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))


def cut_worker(job_id: str, source_filename: str, ts_from: str, ts_to: str,
               mode: str = "normal"):
    temp_file = None
    try:
        if not validate_timestamp(ts_from):
            raise ValueError(f"Invalid start timestamp: {ts_from}. Use HH:MM:SS or HH:MM:SS.mmm")
        if not validate_timestamp(ts_to):
            raise ValueError(f"Invalid end timestamp: {ts_to}. Use HH:MM:SS or HH:MM:SS.mmm")

        source = DOWNLOADS / source_filename
        if not source.exists():
            raise RuntimeError(f"Source file not found: {source_filename}")

        start_s  = _timestamp_to_seconds(ts_from)
        end_s    = _timestamp_to_seconds(ts_to)
        if end_s <= start_s:
            raise ValueError("End timestamp must be greater than start timestamp")
        duration = str(end_s - start_s)

        safe_from = ts_from.replace(":", "-").replace(".", "_")
        safe_to   = ts_to.replace(":", "-").replace(".", "_")
        clip_name = f"clip_{uuid.uuid4().hex[:8]}.mp4"
        out_file  = CLIPS / clip_name

        if mode == "9:16":
            # Step 1: fast stream-copy cut to temp — minimal memory
            temp_file = TEMP / f"tmp_{uuid.uuid4().hex[:8]}.mp4"
            cut_cmd   = [
                "ffmpeg", "-y",
                "-ss", ts_from, "-i", str(source),
                "-t", duration,
                "-c", "copy", "-avoid_negative_ts", "make_zero",
                str(temp_file)
            ]
            cr = subprocess.run(cut_cmd, capture_output=True, text=True, timeout=120)
            if cr.returncode != 0:
                raise RuntimeError(f"Cut step failed: {cr.stderr.strip()[-300:]}")

            # Step 2: re-encode only the small clip
            video_filter = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
            cmd = [
                "ffmpeg", "-y",
                "-i", str(temp_file),
                "-vf", video_filter,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out_file)
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-ss", ts_from, "-i", str(source),
                "-t", duration,
                "-c", "copy", "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(out_file)
            ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        if result.returncode != 0:
            if result.returncode == -9:
                raise RuntimeError("FFmpeg killed by OS (out of memory). Try a shorter clip or upgrade server RAM.")
            raise RuntimeError(result.stderr.strip()[-600:])

        if not out_file.exists() or out_file.stat().st_size < 1000:
            raise RuntimeError(f"Output file missing or empty. stderr: {result.stderr.strip()[-300:]}")

        job_set(job_id, "done", {
            "clip_filename": clip_name,
            "from": ts_from,
            "to":   ts_to,
            "mode": mode,
            "size_mb": get_file_size_mb(out_file),
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))
    finally:
        if temp_file and temp_file.exists():
            temp_file.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# INLINE HTML
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ClipForge</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0f0f11;--surface:#1a1a1f;--surface2:#222228;--surface3:#1e1e24;
  --border:#2e2e38;--accent:#7c5cfc;--accent2:#fc5c7d;
  --text:#e8e8f0;--muted:#6b6b80;--ok:#4caf88;--err:#fc5c5c;--del:#e06c75;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code','Consolas',monospace;font-size:13px;min-height:100vh}
header{padding:15px 32px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:14px}
header h1{font-size:16px;font-weight:700;letter-spacing:.06em;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.badge{font-size:10px;color:var(--muted);border:1px solid var(--border);padding:2px 8px;border-radius:4px}
.layout{display:grid;grid-template-columns:1fr 380px;gap:24px;max-width:1280px;margin:28px auto;padding:0 24px}
.col-left{display:flex;flex-direction:column;gap:20px}
.col-right{display:flex;flex-direction:column;gap:20px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.card-hd{padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.card-hd .num{width:22px;height:22px;border-radius:6px;background:var(--accent);color:#fff;font-size:10px;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.card-hd .ttl{font-size:13px;font-weight:600;flex:1}
.card-bd{padding:18px;display:flex;flex-direction:column;gap:12px}
.lbl{font-size:10px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;display:block;margin-bottom:5px}
input[type=text]{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:9px 12px;border-radius:8px;font-family:inherit;font-size:13px;outline:none;transition:border-color .15s}
input[type=text]:focus{border-color:var(--accent)}
.row{display:flex;gap:8px}
.row input{flex:1}
button{background:var(--accent);color:#fff;border:none;padding:9px 18px;border-radius:8px;font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:opacity .15s,transform .1s;white-space:nowrap}
button:hover{opacity:.85}
button:active{transform:scale(.97)}
button.sec{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
button.ok-btn{background:#2a6b4a}
button.warn-btn{background:#3b3b5c;color:#a0a0c0}
button.del-btn{background:rgba(224,108,117,0.12);border:1px solid var(--del);color:var(--del);padding:4px 10px;font-size:10px}
button:disabled{opacity:.35;cursor:not-allowed}
.pw{background:var(--border);border-radius:4px;height:3px;overflow:hidden;margin-top:2px}
.pb{height:100%;width:0%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:4px;transition:width .3s}
.pb.spin{animation:spin 1.4s ease-in-out infinite;width:35%!important}
@keyframes spin{0%{transform:translateX(-100%)}100%{transform:translateX(380%)}}
.msg{font-size:11px;color:var(--muted);min-height:15px;line-height:1.5}
.msg.ok{color:var(--ok)}.msg.err{color:var(--err)}

/* Video info box */
.vinfo{background:var(--surface2);border:1px solid var(--border);border-radius:10px;padding:14px;display:none;gap:12px}
.vinfo.show{display:flex}
.vinfo-thumb{width:100px;height:58px;object-fit:cover;border-radius:6px;background:var(--border);flex-shrink:0}
.vinfo-body{flex:1;min-width:0}
.vinfo-title{font-size:12px;font-weight:600;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.vinfo-meta{font-size:10px;color:var(--muted);margin-bottom:8px}
.vinfo-actions{display:flex;gap:6px;flex-wrap:wrap}

/* Exists banner */
.exists-banner{background:rgba(76,175,136,0.1);border:1px solid rgba(76,175,136,0.3);border-radius:8px;padding:10px 14px;font-size:11px;color:var(--ok);display:none;align-items:center;gap:8px}
.exists-banner.show{display:flex}
.exists-banner button{padding:5px 12px;font-size:11px}

/* Subtitle accordion */
.sub-accordion{border:1px solid var(--border);border-radius:8px;overflow:hidden}
.sub-toggle{width:100%;background:var(--surface2);border:none;color:var(--text);padding:10px 14px;font-family:inherit;font-size:11px;font-weight:600;cursor:pointer;display:flex;align-items:center;justify-content:space-between;text-align:left}
.sub-toggle:hover{background:var(--surface3)}
.sub-toggle .arrow{transition:transform .2s;font-size:10px;color:var(--muted)}
.sub-toggle.open .arrow{transform:rotate(180deg)}
.sub-body{display:none;padding:12px;background:var(--bg);border-top:1px solid var(--border)}
.sub-body.open{display:block}
.sub-actions{display:flex;gap:6px;margin-bottom:8px}
.sub-copy-btn{background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:5px 12px;border-radius:6px;font-size:10px;font-family:inherit;cursor:pointer}
.sub-copy-btn:hover{border-color:var(--accent);color:var(--accent)}
textarea.sub-ta{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:10px;border-radius:6px;font-family:inherit;font-size:11px;resize:vertical;outline:none}

/* Clip editor */
.ts-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.mode-row{display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.mode-row label{display:flex;align-items:center;gap:5px;font-size:11px;cursor:pointer;color:var(--muted)}
.mode-row input[type=radio]{accent-color:var(--accent)}
.mode-row label:has(input:checked){color:var(--text)}
.clip-result{background:var(--surface2);border:1px solid var(--ok);border-radius:8px;padding:12px 14px;display:none}
.clip-result.show{display:flex;align-items:center;justify-content:space-between;gap:10px}
.clip-result .cr-name{font-size:11px;color:var(--ok);font-weight:600}
.clip-result .cr-meta{font-size:10px;color:var(--muted);margin-top:2px}

/* Library */
.lib-section-label{font-size:10px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;padding:4px 0;border-bottom:1px solid var(--border);margin-bottom:8px}
.file-list{display:flex;flex-direction:column;gap:6px;max-height:340px;overflow-y:auto}
.file-list::-webkit-scrollbar{width:4px}
.file-list::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
.fitem{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:9px 12px;display:flex;align-items:center;gap:8px;cursor:pointer;transition:border-color .15s}
.fitem:hover{border-color:var(--accent)}
.fitem.active{border-color:var(--accent);background:rgba(124,92,252,0.08)}
.fitem-info{flex:1;min-width:0}
.fitem-name{font-size:11px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fitem-meta{font-size:10px;color:var(--muted);margin-top:2px}
.fitem-actions{display:flex;gap:5px;flex-shrink:0}
.fitem-dl{font-size:10px;color:var(--accent);text-decoration:none;padding:3px 8px;border:1px solid var(--accent);border-radius:5px}
.fitem-dl:hover{background:var(--accent);color:#fff}
.hint{font-size:11px;color:var(--muted);line-height:1.6}
.sep{height:1px;background:var(--border)}
</style>
</head>
<body>
<header>
  <h1>⬡ CLIPFORGE</h1>
  <span class="badge">yt-dlp · FFmpeg · Subtitles</span>
</header>

<div class="layout">
<div class="col-left">

  <!-- CARD 1: VIDEO SOURCE -->
  <div class="card">
    <div class="card-hd">
      <div class="num">1</div>
      <div class="ttl">Video Source</div>
    </div>
    <div class="card-bd">
      <div>
        <label class="lbl">YouTube URL</label>
        <div class="row">
          <input type="text" id="url-input" placeholder="https://youtube.com/watch?v=..."/>
          <button id="check-btn" onclick="startCheck()">Check</button>
        </div>
      </div>
      <div class="pw"><div class="pb" id="check-pb"></div></div>
      <div class="msg" id="check-msg"></div>

      <!-- Already exists banner -->
      <div class="exists-banner" id="exists-banner">
        <span>✓ Already downloaded</span>
        <button class="sec" onclick="useExisting()">Use This Video</button>
        <button class="warn-btn" onclick="forceDownload()">Re-download</button>
      </div>

      <!-- Video info -->
      <div class="vinfo" id="vinfo">
        <img class="vinfo-thumb" id="vinfo-thumb" src="" alt=""/>
        <div class="vinfo-body">
          <div class="vinfo-title" id="vinfo-title"></div>
          <div class="vinfo-meta" id="vinfo-meta"></div>
          <div class="vinfo-actions">
            <button id="dl-btn" onclick="startDownload()" style="display:none">⬇ Download Video</button>
            <button class="warn-btn" onclick="fetchSubtitlesOnly()">📝 Subtitles Only</button>
          </div>
        </div>
      </div>

      <div class="pw"><div class="pb" id="dl-pb"></div></div>
      <div class="msg" id="dl-msg"></div>

      <!-- Subtitle accordion -->
      <div class="sub-accordion" id="sub-accordion" style="display:none">
        <button class="sub-toggle" id="sub-toggle" onclick="toggleSub()">
          <span>📝 Transcript (English)</span>
          <span class="arrow">▼</span>
        </button>
        <div class="sub-body" id="sub-body">
          <div class="sub-actions">
            <button class="sub-copy-btn" onclick="copySubText()">Copy Text</button>
            <button class="sub-copy-btn" onclick="fetchAndCopySegments()">Copy Segments JSON</button>
          </div>
          <textarea class="sub-ta" id="subtitle-text" rows="7" readonly></textarea>
        </div>
      </div>
    </div>
  </div>

  <!-- CARD 2: CUT CLIP -->
  <div class="card">
    <div class="card-hd">
      <div class="num">2</div>
      <div class="ttl">Cut Clip</div>
    </div>
    <div class="card-bd">
      <div class="hint" id="cut-hint">Select or download a video first, then set timestamps.</div>
      <div class="ts-grid">
        <div>
          <label class="lbl">From (HH:MM:SS.mmm)</label>
          <input type="text" id="ts-from" placeholder="00:00:00.000"/>
        </div>
        <div>
          <label class="lbl">To (HH:MM:SS.mmm)</label>
          <input type="text" id="ts-to" placeholder="00:00:30.000"/>
        </div>
      </div>
      <div>
        <label class="lbl">Output Format</label>
        <div class="mode-row">
          <label><input type="radio" name="cut-mode" value="normal" checked/> Original (stream copy, instant)</label>
          <label><input type="radio" name="cut-mode" value="9:16"/> 9:16 Vertical (1080×1920, letterbox)</label>
        </div>
      </div>
      <button id="cut-btn" onclick="startCut()" disabled>✂ Cut Clip</button>
      <div class="pw"><div class="pb" id="cut-pb"></div></div>
      <div class="msg" id="cut-msg"></div>
      <div class="clip-result" id="clip-result">
        <div>
          <div class="cr-name" id="cr-name"></div>
          <div class="cr-meta" id="cr-meta"></div>
        </div>
        <button class="ok-btn" id="cut-dl-btn" onclick="downloadClip()">⬇ Download</button>
      </div>
    </div>
  </div>

</div>
<div class="col-right">

  <!-- CARD 3: LIBRARY -->
  <div class="card">
    <div class="card-hd">
      <div class="num">3</div>
      <div class="ttl">Library</div>
      <button class="sec" onclick="loadLibrary()" style="margin-left:auto;padding:5px 10px;font-size:11px">↻</button>
    </div>
    <div class="card-bd">
      <div class="lib-section-label">Downloaded Videos</div>
      <div class="file-list" id="videos-list"><div class="msg">Loading…</div></div>
      <div class="sep"></div>
      <div class="lib-section-label">Clips</div>
      <div class="file-list" id="clips-list"><div class="msg">Loading…</div></div>
    </div>
  </div>

</div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let state = {
  videoId: null,
  title: null,
  durationStr: null,
  durationSec: null,
  thumbnail: null,
  filename: null,        // active filename for cutting
  clipFilename: null,
  subtitleText: '',
  checkData: null,       // raw data from last check
};

// ── Helpers ────────────────────────────────────────────────────────────────
function setMsg(id, cls, txt) {
  const el = document.getElementById(id);
  el.className = 'msg' + (cls ? ' ' + cls : '');
  el.textContent = txt;
}
function setPb(id, on) {
  const pb = document.getElementById(id);
  on ? (pb.classList.add('spin'), pb.style.width='35%')
     : (pb.classList.remove('spin'));
}
function escapeHtml(s) {
  return s.replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
}
function poll(jid, pbId, msgId, onDone, onFail) {
  setPb(pbId, true);
  const iv = setInterval(async () => {
    try {
      const d = await (await fetch('/api/job/' + jid)).json();
      if (d.state === 'done') {
        clearInterval(iv); setPb(pbId, false);
        document.getElementById(pbId).style.width = '100%';
        onDone(d.data);
      } else if (d.state === 'error') {
        clearInterval(iv); setPb(pbId, false);
        document.getElementById(pbId).style.width = '0%';
        setMsg(msgId, 'err', '✗ ' + (d.error || 'error'));
        if (onFail) onFail();
      }
    } catch(e) {
      clearInterval(iv); setPb(pbId, false);
      setMsg(msgId, 'err', '✗ Network error');
    }
  }, 900);
}

// ── Show video info panel ──────────────────────────────────────────────────
function showVinfo(data, showDlBtn) {
  document.getElementById('vinfo-thumb').src   = data.thumbnail || '';
  document.getElementById('vinfo-title').textContent = data.title || '';
  document.getElementById('vinfo-meta').textContent  =
    'Duration: ' + (data.duration_str || '') + (data.size_mb ? '  ·  ' + data.size_mb + ' MB' : '');
  document.getElementById('vinfo').classList.add('show');
  document.getElementById('dl-btn').style.display = showDlBtn ? '' : 'none';
}

// ── STEP 1a: CHECK ─────────────────────────────────────────────────────────
async function startCheck() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return;

  // Reset state
  state = { ...state, videoId: null, title: null, filename: null, clipFilename: null, checkData: null };
  document.getElementById('exists-banner').classList.remove('show');
  document.getElementById('vinfo').classList.remove('show');
  document.getElementById('sub-accordion').style.display = 'none';
  document.getElementById('cut-btn').disabled = true;
  document.getElementById('clip-result').classList.remove('show');
  document.getElementById('dl-pb').style.width = '0%';
  document.getElementById('check-pb').style.width = '0%';
  setMsg('check-msg', '', 'Checking…');
  setMsg('dl-msg', '', '');
  document.getElementById('check-btn').disabled = true;

  try {
    const res  = await fetch('/api/check', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url}) });
    const data = await res.json();
    poll(data.job_id, 'check-pb', 'check-msg', (d) => {
      document.getElementById('check-btn').disabled = false;
      state.checkData   = d;
      state.videoId     = d.video_id;
      state.title       = d.title;
      state.durationStr = d.duration_str;
      state.durationSec = d.duration_sec;
      state.thumbnail   = d.thumbnail;

      if (d.exists) {
        state.filename = d.filename;
        setMsg('check-msg', 'ok', '✓ Found in library');
        showVinfo({ ...d, size_mb: d.size_mb }, false);
        document.getElementById('exists-banner').classList.add('show');
      } else {
        setMsg('check-msg', '', 'Not downloaded yet');
        showVinfo(d, true);
      }
    }, () => { document.getElementById('check-btn').disabled = false; });
  } catch(e) {
    document.getElementById('check-btn').disabled = false;
    setMsg('check-msg', 'err', '✗ Request failed');
  }
}

// Use existing file for cutting — no download needed
function useExisting() {
  document.getElementById('cut-btn').disabled = false;
  document.getElementById('cut-hint').textContent =
    'Ready: ' + (state.filename || '');
  document.getElementById('exists-banner').classList.remove('show');
  setMsg('check-msg', 'ok', '✓ Using existing: ' + state.filename);
}

// Force re-download even if file exists
function forceDownload() {
  document.getElementById('exists-banner').classList.remove('show');
  startDownload();
}

// ── STEP 1b: DOWNLOAD ──────────────────────────────────────────────────────
async function startDownload() {
  if (!state.videoId) { setMsg('dl-msg', 'err', '✗ Check a URL first'); return; }
  document.getElementById('dl-btn').disabled = true;
  document.getElementById('dl-pb').style.width = '0%';
  setMsg('dl-msg', '', 'Downloading…');

  try {
    const res  = await fetch('/api/download', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        video_id:     state.videoId,
        title:        state.title,
        duration_sec: state.durationSec,
        duration_str: state.durationStr,
      })
    });
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      document.getElementById('dl-btn').disabled = false;
      state.filename     = d.filename;
      state.subtitleText = d.subtitle_text || '';
      setMsg('dl-msg', 'ok', '✓ Downloaded: ' + d.filename);
      showVinfo({ ...state, size_mb: d.size_mb }, false);
      document.getElementById('cut-btn').disabled = false;
      document.getElementById('cut-hint').textContent = 'Ready: ' + d.filename;
      if (state.subtitleText) {
        document.getElementById('subtitle-text').value = state.subtitleText;
        document.getElementById('sub-accordion').style.display = '';
      }
      loadLibrary();
    }, () => { document.getElementById('dl-btn').disabled = false; });
  } catch(e) {
    document.getElementById('dl-btn').disabled = false;
    setMsg('dl-msg', 'err', '✗ Request failed');
  }
}

// ── Subtitles only ─────────────────────────────────────────────────────────
async function fetchSubtitlesOnly() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) { setMsg('dl-msg', 'err', '✗ Enter a URL first'); return; }
  setMsg('dl-msg', '', 'Fetching subtitles…');
  document.getElementById('dl-pb').style.width = '0%';
  try {
    const res  = await fetch('/api/subtitles-only', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url}) });
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      state.subtitleText = d.subtitle_text || '';
      setMsg('dl-msg', 'ok', '✓ Subtitles ready');
      document.getElementById('subtitle-text').value = state.subtitleText || '(none found)';
      document.getElementById('sub-accordion').style.display = '';
    });
  } catch(e) { setMsg('dl-msg', 'err', '✗ Request failed'); }
}

async function fetchAndCopySegments() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) { setMsg('dl-msg', 'err', '✗ Enter a URL'); return; }
  setMsg('dl-msg', '', 'Fetching segments…');
  try {
    const res  = await fetch('/api/subtitles-segments', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url}) });
    const data = await res.json();
    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      const json = JSON.stringify(d.segments, null, 2);
      navigator.clipboard.writeText(json);
      document.getElementById('subtitle-text').value = json;
      document.getElementById('sub-accordion').style.display = '';
      // Open it
      const body = document.getElementById('sub-body');
      const tog  = document.getElementById('sub-toggle');
      body.classList.add('open'); tog.classList.add('open');
      setMsg('dl-msg', 'ok', `✓ ${d.segments.length} segments copied to clipboard`);
    });
  } catch(e) { setMsg('dl-msg', 'err', '✗ Request failed'); }
}

// ── Subtitle accordion ─────────────────────────────────────────────────────
function toggleSub() {
  document.getElementById('sub-body').classList.toggle('open');
  document.getElementById('sub-toggle').classList.toggle('open');
}
function copySubText() {
  const ta = document.getElementById('subtitle-text');
  navigator.clipboard.writeText(ta.value).then(() => {
    setMsg('dl-msg', 'ok', '✓ Text copied to clipboard');
  });
}

// ── STEP 2: CUT ────────────────────────────────────────────────────────────
async function startCut() {
  if (!state.filename) { setMsg('cut-msg', 'err', '✗ No video selected'); return; }
  const from = document.getElementById('ts-from').value.trim();
  const to   = document.getElementById('ts-to').value.trim();
  if (!from || !to) { setMsg('cut-msg', 'err', '✗ Both timestamps required'); return; }
  const tsRe = /^\d{1,2}:\d{1,2}:\d{2}(?:\.\d{1,3})?$/;
  if (!tsRe.test(from)) { setMsg('cut-msg', 'err', '✗ Invalid From timestamp'); return; }
  if (!tsRe.test(to))   { setMsg('cut-msg', 'err', '✗ Invalid To timestamp'); return; }

  const cutMode = document.querySelector('input[name="cut-mode"]:checked')?.value || 'normal';
  state.clipFilename = null;
  document.getElementById('cut-btn').disabled = true;
  document.getElementById('clip-result').classList.remove('show');
  document.getElementById('cut-pb').style.width = '0%';
  setMsg('cut-msg', '', cutMode === '9:16' ? 'Cutting and converting 9:16…' : 'Cutting clip…');

  try {
    const res  = await fetch('/api/cut', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ source_filename: state.filename, ts_from: from, ts_to: to, mode: cutMode })
    });
    const data = await res.json();
    poll(data.job_id, 'cut-pb', 'cut-msg', (d) => {
      state.clipFilename = d.clip_filename;
      document.getElementById('cut-btn').disabled = false;
      setMsg('cut-msg', 'ok', `✓ Done — ${d.from} → ${d.to}`);
      document.getElementById('cr-name').textContent = d.clip_filename;
      document.getElementById('cr-meta').textContent = (d.size_mb || '') + (d.size_mb ? ' MB' : '') + (d.mode === '9:16' ? ' · 9:16 vertical' : '');
      document.getElementById('clip-result').classList.add('show');
      loadLibrary();
    }, () => { document.getElementById('cut-btn').disabled = false; });
  } catch(e) {
    document.getElementById('cut-btn').disabled = false;
    setMsg('cut-msg', 'err', '✗ Request failed');
  }
}

function downloadClip() {
  if (state.clipFilename)
    window.location.href = '/api/download-file/clip/' + encodeURIComponent(state.clipFilename);
}

// ── Library: click to select video for cutting ─────────────────────────────
function selectVideo(filename) {
  state.filename = filename;
  document.getElementById('cut-btn').disabled = false;
  document.getElementById('cut-hint').textContent = 'Ready: ' + filename;
  setMsg('cut-msg', '', '');
  document.getElementById('clip-result').classList.remove('show');
  // Highlight selected
  document.querySelectorAll('#videos-list .fitem').forEach(el => {
    el.classList.toggle('active', el.dataset.fn === filename);
  });
}

async function deleteVideo(filename, ev) {
  ev.stopPropagation();
  if (!confirm(`Delete "${filename}"?`)) return;
  try {
    const res = await fetch('/api/downloads/' + encodeURIComponent(filename), { method: 'DELETE' });
    if (res.ok) {
      if (state.filename === filename) {
        state.filename = null;
        document.getElementById('cut-btn').disabled = true;
        document.getElementById('cut-hint').textContent = 'Select or download a video first.';
      }
      loadLibrary();
    }
  } catch(e) {}
}

async function deleteClip(filename, ev) {
  ev.stopPropagation();
  if (!confirm(`Delete "${filename}"?`)) return;
  try {
    const res = await fetch('/api/clips/' + encodeURIComponent(filename), { method: 'DELETE' });
    if (res.ok) loadLibrary();
  } catch(e) {}
}

async function loadLibrary() {
  const vl = document.getElementById('videos-list');
  const cl = document.getElementById('clips-list');
  try {
    const data = await (await fetch('/api/downloads/list')).json();

    if (!data.videos.length) {
      vl.innerHTML = '<div class="msg">No videos yet.</div>';
    } else {
      vl.innerHTML = data.videos.map(f => `
        <div class="fitem" data-fn="${escapeHtml(f.filename)}" onclick="selectVideo('${escapeHtml(f.filename)}')">
          <div class="fitem-info">
            <div class="fitem-name">${escapeHtml(f.filename)}</div>
            <div class="fitem-meta">${f.size_mb} MB · ${f.modified}</div>
          </div>
          <div class="fitem-actions" onclick="event.stopPropagation()">
            <a class="fitem-dl" href="/api/download-file/video/${encodeURIComponent(f.filename)}" download>⬇</a>
            <button class="del-btn" onclick="deleteVideo('${escapeHtml(f.filename)}', event)">🗑</button>
          </div>
        </div>`).join('');
      // Re-apply active state
      if (state.filename) {
        document.querySelectorAll('#videos-list .fitem').forEach(el => {
          el.classList.toggle('active', el.dataset.fn === state.filename);
        });
      }
    }

    if (!data.clips.length) {
      cl.innerHTML = '<div class="msg">No clips yet.</div>';
    } else {
      cl.innerHTML = data.clips.map(f => `
        <div class="fitem">
          <div class="fitem-info">
            <div class="fitem-name">${escapeHtml(f.filename)}</div>
            <div class="fitem-meta">${f.size_mb} MB · ${f.modified}</div>
          </div>
          <div class="fitem-actions">
            <a class="fitem-dl" href="/api/download-file/clip/${encodeURIComponent(f.filename)}" download>⬇</a>
            <button class="del-btn" onclick="deleteClip('${escapeHtml(f.filename)}', event)">🗑</button>
          </div>
        </div>`).join('');
    }
  } catch(e) {
    vl.innerHTML = '<div class="msg err">Failed to load.</div>';
    cl.innerHTML = '<div class="msg err">Failed to load.</div>';
  }
}

document.getElementById('url-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') startCheck();
});

loadLibrary();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# MODELS
# ---------------------------------------------------------------------------
class CheckRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    video_id: str
    title: str
    duration_sec: float
    duration_str: str

class CutRequest(BaseModel):
    source_filename: str
    ts_from: str
    ts_to: str
    mode: str = "normal"

    @validator('ts_from', 'ts_to')
    def validate_ts(cls, v):
        if not validate_timestamp(v):
            raise ValueError(f"Invalid timestamp: {v}. Use HH:MM:SS or HH:MM:SS.mmm")
        return v

class SubtitleRequest(BaseModel):
    url: str

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML)

@app.post("/api/check")
async def api_check(req: CheckRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=check_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/download")
async def api_download(req: DownloadRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(
        target=download_worker,
        args=(job_id, None, req.video_id, req.title, req.duration_sec, req.duration_str),
        daemon=True
    ).start()
    return {"job_id": job_id}

@app.post("/api/subtitles-only")
async def api_subtitles_only(req: SubtitleRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=subtitle_only_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/subtitles-segments")
async def api_subtitles_segments(req: SubtitleRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=subtitle_segments_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/cut")
async def api_cut(req: CutRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(
        target=cut_worker,
        args=(job_id, req.source_filename, req.ts_from, req.ts_to, req.mode),
        daemon=True
    ).start()
    return {"job_id": job_id}

@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    return JOBS.get(job_id, {"state": "not_found"})

@app.get("/api/downloads/list")
async def list_downloads():
    videos = []
    for f in sorted(DOWNLOADS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        videos.append({
            "filename": f.name,
            "size_mb":  round(stat.st_size / (1024*1024), 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    clips = []
    for f in sorted(CLIPS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        clips.append({
            "filename": f.name,
            "size_mb":  round(stat.st_size / (1024*1024), 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return {"videos": videos, "clips": clips}

@app.delete("/api/downloads/{filename}")
async def delete_download(filename: str):
    path = DOWNLOADS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"message": f"Deleted {filename}"}

@app.delete("/api/clips/{filename}")
async def delete_clip_file(filename: str):
    path = CLIPS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    path.unlink()
    return {"message": f"Deleted {filename}"}

@app.get("/api/download-file/video/{filename}")
async def download_video(filename: str):
    path = DOWNLOADS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(path), media_type="video/mp4", filename=filename,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})

@app.get("/api/download-file/clip/{filename}")
async def download_clip(filename: str):
    path = CLIPS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(path), media_type="video/mp4", filename=filename,
                        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"})
