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
BASE_DIR    = Path(__file__).parent
COOKIES     = BASE_DIR / "cookies.txt"
DOWNLOADS   = BASE_DIR / "downloads"
CLIPS       = BASE_DIR / "clips"
TEMP        = BASE_DIR / "temp"

DOWNLOADS.mkdir(exist_ok=True)
CLIPS.mkdir(exist_ok=True)
TEMP.mkdir(exist_ok=True)

app = FastAPI(title="ClipForge")

# ---------------------------------------------------------------------------
# IN-MEMORY JOB STORE
# ---------------------------------------------------------------------------
JOBS: dict = {}

def job_set(job_id: str, state: str, data: dict = None, error: str = None):
    JOBS[job_id] = {"state": state, "data": data or {}, "error": error}

# ---------------------------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------------------------
def seconds_to_ts(s: float) -> str:
    """Convert seconds to HH:MM:SS.mmm (same as original Python helper)."""
    s = round(s, 3)
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = s % 60
    return f"{h:02d}:{m:02d}:{sec:06.3f}"

def _timestamp_to_seconds(ts: str) -> float:
    parts = ts.split(":")
    if len(parts) != 3:
        raise ValueError("Timestamp must be in HH:MM:SS format")
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = float(parts[2])
    return hours * 3600 + minutes * 60 + seconds

def _calculate_duration(ts_from: str, ts_to: str) -> str:
    start_seconds = _timestamp_to_seconds(ts_from)
    end_seconds = _timestamp_to_seconds(ts_to)
    if end_seconds <= start_seconds:
        raise ValueError("'to' timestamp must be greater than 'from' timestamp")
    duration = end_seconds - start_seconds
    return str(duration)

def ts_to_seconds(ts: str) -> float:
    """Convert HH:MM:SS[.mmm] to seconds."""
    parts = ts.split(':')
    if len(parts) != 3:
        raise ValueError("Timestamp must be HH:MM:SS[.mmm]")
    h = int(parts[0])
    m = int(parts[1])
    # Split seconds part (may contain milliseconds)
    sec_part = parts[2]
    if '.' in sec_part:
        s, ms = sec_part.split('.')
        seconds = float(s) + float(ms) / 1000
    else:
        seconds = float(sec_part)
    return h * 3600 + m * 60 + seconds

def strip_milliseconds(ts: str) -> str:
    """Remove milliseconds from timestamp if present. Returns HH:MM:SS."""
    # Split at dot and take first part
    return ts.split('.')[0]

def validate_timestamp(ts: str) -> bool:
    """Check if timestamp matches HH:MM:SS or HH:MM:SS.mmm (optional milliseconds)."""
    pattern = r'^\d{1,2}:\d{1,2}:\d{2}(?:\.\d{1,3})?$'
    if not re.match(pattern, ts):
        return False
    try:
        ts_to_seconds(ts)
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

# ---------------------------------------------------------------------------
# WORKERS
# ---------------------------------------------------------------------------
def download_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])

        meta = json.loads(meta_res.stdout.strip().splitlines()[-1])
        video_id = meta.get("id", "unknown")
        title = meta.get("title", "untitled")
        duration_raw = meta.get("duration", 0)
        try:
            duration_sec = float(duration_raw)
        except (TypeError, ValueError):
            duration_sec = 0
        duration_str = seconds_to_ts(duration_sec) if duration_sec > 0 else "00:00:00.000"

        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:60]
        out_file = DOWNLOADS / f"{video_id}_{safe_title}.mp4"

        if out_file.exists():
            vtt_path = out_file.with_suffix('.en.vtt')
            sub_text = extract_plain_text_from_vtt(vtt_path) if vtt_path.exists() else ""
            job_set(job_id, "done", {
                "video_id": video_id,
                "title": title,
                "duration_sec": duration_sec,
                "duration_str": duration_str,
                "filename": out_file.name,
                "subtitle_text": sub_text,
                "size_mb": get_file_size_mb(out_file),
            })
            return

        dl_cmd = ytdlp_base() + [
            "-S", "vcodec:h264,res,acodec:aac",
            "--merge-output-format", "mp4",
            "--no-playlist",
            "-o", str(out_file),
            url
        ]
        result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-400:])

        vtt_path = out_file.with_suffix('.en.vtt')
        sub_cmd = ytdlp_base() + [
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en",
            "--sub-format", "vtt",
            "-o", str(out_file.with_suffix('')),
            url
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
            "subtitle_text": sub_text,
            "size_mb": get_file_size_mb(out_file),
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

def subtitle_only_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])
        meta = json.loads(meta_res.stdout.strip().splitlines()[-1])
        title = meta.get("title", "untitled")

        tmp_base = TEMP / f"sub_{job_id}"
        sub_cmd = ytdlp_base() + [
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en",
            "--sub-format", "vtt",
            "-o", str(tmp_base),
            url
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)
        vtt_path = Path(str(tmp_base) + ".en.vtt")
        sub_text = extract_plain_text_from_vtt(vtt_path)
        vtt_path.unlink(missing_ok=True)

        job_set(job_id, "done", {
            "title": title,
            "subtitle_text": sub_text,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

def subtitle_segments_worker(job_id: str, url: str):
    try:
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])
        meta = json.loads(meta_res.stdout.strip().splitlines()[-1])
        title = meta.get("title", "untitled")

        tmp_base = TEMP / f"segments_{job_id}"
        sub_cmd = ytdlp_base() + [
            "--skip-download",
            "--write-auto-subs",
            "--write-subs",
            "--sub-langs", "en",
            "--sub-format", "json3",
            "-o", str(tmp_base),
            url
        ]
        subprocess.run(sub_cmd, capture_output=True, timeout=60)

        json3_path = Path(str(tmp_base) + ".en.json3")
        if not json3_path.exists():
            raise RuntimeError("No JSON3 subtitles found (English).")

        with open(json3_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        segments = []
        for event in data.get("events", []):
            start_ms = event.get("tStartMs", 0)
            duration_ms = event.get("dDurationMs", 0)
            text_parts = [seg.get("utf8", "") for seg in event.get("segs", [])]
            text = "".join(text_parts).strip()
            if not text or text == "\n":
                continue

            start_sec = start_ms / 1000.0
            end_sec = (start_ms + duration_ms) / 1000.0
            segments.append({
                "start": seconds_to_ts(start_sec),
                "end": seconds_to_ts(end_sec),
                "text": text
            })

        json3_path.unlink(missing_ok=True)

        job_set(job_id, "done", {
            "segments": segments,
            "title": title,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))
        

def cut_worker(job_id: str, source_filename: str, ts_from: str, ts_to: str, mode: str = "normal"):
    temp_file = None  # Kept for compatibility, though no longer needed for 9:16 mode
    try:
        if not validate_timestamp(ts_from):
            raise ValueError(
                f"Invalid start timestamp: {ts_from}. "
                "Use HH:MM:SS or HH:MM:SS.mmm"
            )

        if not validate_timestamp(ts_to):
            raise ValueError(
                f"Invalid end timestamp: {ts_to}. "
                "Use HH:MM:SS or HH:MM:SS.mmm"
            )

        source = DOWNLOADS / source_filename

        if not source.exists():
            raise RuntimeError(f"Source file not found: {source_filename}")

        start_seconds = _timestamp_to_seconds(ts_from)
        end_seconds = _timestamp_to_seconds(ts_to)

        if end_seconds <= start_seconds:
            raise ValueError("End timestamp must be greater than start timestamp")

        duration = str(end_seconds - start_seconds)

        safe_from = ts_from.replace(":", "-").replace(".", "_")
        safe_to = ts_to.replace(":", "-").replace(".", "_")

        clip_name = f"clip_{uuid.uuid4().hex[:8]}_{safe_from}_{safe_to}.mp4"
        out_file = CLIPS / clip_name
        
        if mode == "9:16":
            # Letterbox pipeline (Fits the entire source video, adds black bars)
            video_filter = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
            cmd = [
                "ffmpeg", "-y",
                "-threads", "1",
                "-i", str(source),
                "-ss", ts_from,
                "-t", duration,
                "-vf", video_filter,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                str(out_file)
            ]
        else:
            # Normal stream copy mode
            cmd = [
                "ffmpeg", "-y",
                "-ss", ts_from,
                "-i", str(source),
                "-t", duration,
                "-c", "copy",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                str(out_file)
            ]

        # Single execution point for both modes
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

        print(f"[CUT DEBUG] returncode={result.returncode} | file_exists={out_file.exists()} | size={out_file.stat().st_size if out_file.exists() else 0}")
        print(f"[CUT DEBUG] stderr tail: {result.stderr.strip()[-200:]}")

        if result.returncode != 0:
            if result.returncode == -9:
                raise RuntimeError("FFmpeg process was killed by the system (Out of Memory). Try upgrading server RAM or adding a swap file.")
            raise RuntimeError(result.stderr.strip()[-600:])

        if not out_file.exists() or out_file.stat().st_size < 1000:
            raise RuntimeError(f"Output file missing or empty. FFmpeg stderr: {result.stderr.strip()[-400:]}")

        job_set(job_id, "done", {
            "clip_filename": clip_name,
            "from": ts_from,
            "to": ts_to,
        })

    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

    finally:
        if temp_file and temp_file.exists():
            temp_file.unlink(missing_ok=True)



# ---------------------------------------------------------------------------
# INLINE HTML (duration uses backend formatted string)
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
  --bg:#0f0f11;--surface:#1a1a1f;--surface2:#222228;
  --border:#2e2e38;--accent:#7c5cfc;--accent2:#fc5c7d;
  --text:#e8e8f0;--muted:#6b6b80;--ok:#4caf88;--err:#fc5c5c;
  --delete:#e06c75;
}
body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code','Consolas',monospace;font-size:13px;min-height:100vh}
header{padding:16px 32px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:14px;flex-wrap:wrap}
header h1{font-size:17px;font-weight:700;letter-spacing:.06em;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.badge{font-size:10px;color:var(--muted);border:1px solid var(--border);padding:2px 8px;border-radius:4px}
.main{max-width:1200px;margin:40px auto;padding:0 24px;display:flex;flex-direction:column;gap:28px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.card-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.card-header .num{width:24px;height:24px;border-radius:6px;background:var(--accent);color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center}
.card-header .title{font-size:13px;font-weight:600;flex:1}
.card-body{padding:20px;display:flex;flex-direction:column;gap:14px}
label.lbl{font-size:10px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;display:block;margin-bottom:6px}
input[type=text]{width:100%;background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:10px 13px;border-radius:8px;font-family:inherit;font-size:13px;outline:none;transition:border-color .15s}
input[type=text]:focus{border-color:var(--accent)}
.row{display:flex;gap:8px}
.row input{flex:1}
button{background:var(--accent);color:#fff;border:none;padding:10px 20px;border-radius:8px;font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:opacity .15s,transform .1s;white-space:nowrap}
button:hover{opacity:.85}
button:active{transform:scale(.97)}
button.sec{background:var(--surface2);border:1px solid var(--border);color:var(--text)}
button.subtitle-btn{background:#3b3b5c;border-color:#5a5a7a}
button.ok-btn{background:#2a6b4a}
button.delete-btn{background:rgba(224,108,117,0.15);border:1px solid var(--delete);color:var(--delete);padding:4px 10px;font-size:10px}
button:disabled{opacity:.35;cursor:not-allowed}
.pw{background:var(--border);border-radius:4px;height:3px;overflow:hidden}
.pb{height:100%;width:0%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:4px;transition:width .3s}
.pb.spin{animation:spin 1.4s ease-in-out infinite;width:35%!important}
@keyframes spin{0%{transform:translateX(-100%)}100%{transform:translateX(380%)}}
.msg{font-size:11px;color:var(--muted);min-height:16px;line-height:1.5}
.msg.ok{color:var(--ok)}.msg.err{color:var(--err)}
.info-box{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin-top:12px}
.info-box .vt{font-weight:600;font-size:13px;margin-bottom:4px}
.info-box .vm{font-size:11px;color:var(--muted)}
.subtitle-box{background:var(--surface2);border:1px solid var(--border);border-radius:8px;margin-top:12px;padding:12px;position:relative}
.subtitle-box .lbl{font-size:10px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center}
.subtitle-box textarea{width:100%;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:10px;border-radius:6px;font-family:inherit;font-size:12px;resize:vertical}
.copy-btn{background:var(--surface2);border:1px solid var(--border);padding:4px 10px;border-radius:6px;font-size:10px;cursor:pointer}
.copy-btn:hover{background:var(--accent);color:#000}
.file-list{display:flex;flex-direction:column;gap:8px;max-height:400px;overflow-y:auto}
.file-item{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap}
.file-info{flex:1;min-width:150px}
.file-name{font-size:12px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.file-meta{font-size:10px;color:var(--muted);margin-top:3px}
.file-actions{display:flex;gap:6px}
.refresh-btn{background:var(--surface2);border:1px solid var(--border);padding:6px 12px;font-size:11px}
</style>
</head>
<body>
<header>
  <h1>⬡ CLIPFORGE</h1>
  <span class="badge">yt-dlp · FFmpeg · Subtitles</span>
</header>

<div class="main">

  <!-- STEP 1: DOWNLOAD VIDEO + SUBTITLES -->
  <div class="card">
    <div class="card-header">
      <div class="num">1</div>
      <div class="title">Download Video + Subtitles</div>
    </div>
    <div class="card-body">
      <div>
        <label class="lbl">YouTube URL</label>
        <div class="row">
          <input type="text" id="url-input" placeholder="https://youtube.com/watch?v=..."/>
          <button id="dl-btn" onclick="startDownload()">Download</button>
          <button class="subtitle-btn" onclick="fetchSubtitlesOnly()" style="background:#3b3b5c">📝 Subtitles Only</button>
        </div>
      </div>
      <div class="pw"><div class="pb" id="dl-pb"></div></div>
      <div class="msg" id="dl-msg"></div>
      <div class="info-box" id="dl-info" style="display:none">
        <div class="vt" id="dl-title"></div>
        <div class="vm" id="dl-meta"></div>
        <div class="dl-row" style="margin-top:12px">
          <button class="sec" id="dl-full-btn" onclick="downloadFull()" disabled>⬇ Download Video</button>
        </div>
        <div id="subtitle-area" style="display:none" class="subtitle-box">
          <div class="lbl">📝 Transcript (English)
            <div style="display: flex; gap: 8px;">
                <button class="copy-btn" onclick="copySubtitle()">Copy Text</button>
                <button class="copy-btn" onclick="fetchAndCopySegments()">Copy Segments (JSON)</button>
            </div>
          </div>
          <textarea id="subtitle-text" rows="6" readonly placeholder="Subtitle text will appear here..."></textarea>
        </div>
      </div>
    </div>
  </div>

  <!-- STEP 2: CUT CLIP -->
  <div class="card">
    <div class="card-header">
      <div class="num">2</div>
      <div class="title">Cut Clip</div>
    </div>
    <div class="card-body">
      <div class="hint">After downloading a video, set timestamps and cut a clip. Use format HH:MM:SS.mmm (e.g., 01:34:33.000).</div>
      <div class="ts-row">
        <div>
          <label class="lbl">From (HH:MM:SS.mmm)</label>
          <input type="text" id="ts-from" placeholder="00:00:00.000"/>
        </div>
        <div>
          <label class="lbl">To (HH:MM:SS.mmm)</label>
          <input type="text" id="ts-to" placeholder="00:00:30.000"/>
        </div>
      </div>
            <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <label class="lbl" style="margin:0">Output format:</label>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
          <input type="radio" name="cut-mode" value="normal" checked/> Original (stream copy)
        </label>
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
          <input type="radio" name="cut-mode" value="9:16"/> 9:16 Vertical (1080×1920)
        </label>
      </div>
      <div class="row">
        <button id="cut-btn" onclick="startCut()" disabled>✂ Cut Clip</button>
      </div>
      <div class="pw"><div class="pb" id="cut-pb"></div></div>
      <div class="msg" id="cut-msg"></div>
      <div class="info-box" id="cut-info" style="display:none">
        <div class="vt" id="cut-title"></div>
        <div style="margin-top:10px">
          <button class="ok-btn" id="cut-dl-btn" onclick="downloadClip()" disabled>⬇ Download Clip</button>
        </div>
      </div>
    </div>
  </div>

  <!-- DOWNLOADS LIBRARY -->
  <div class="card">
    <div class="card-header">
      <div class="num">3</div>
      <div class="title">Library</div>
      <button class="refresh-btn sec" onclick="loadDownloadsList()" style="margin-left:auto">↻ Refresh</button>
    </div>
    <div class="card-body">
      <div style="font-size:11px;color:var(--muted);margin-bottom:4px">DOWNLOADED VIDEOS</div>
      <div class="file-list" id="downloads-list">
      <div class="msg">Loading...</div>
      </div>
      <div style="font-size:11px;color:var(--muted);margin-top:16px;margin-bottom:4px">CLIPS</div>
      <div class="file-list" id="clips-list">
      <div class="msg">Loading...</div>
      </div>
    </div>
  </div>

</div>

<script>
let currentFilename = null;
let currentClipFilename = null;
let currentSubtitle = '';
let currentFileSize = null;

function setMsg(id, cls, txt) {
  const el = document.getElementById(id);
  el.className = 'msg' + (cls ? ' ' + cls : '');
  el.textContent = txt;
}

function setPb(id, running) {
  const pb = document.getElementById(id);
  if (running) { pb.classList.add('spin'); pb.style.width = '35%'; }
  else          { pb.classList.remove('spin'); }
}

function formatBytes(mb) {
  return mb.toFixed(2) + ' MB';
}

function poll(jobId, pbId, msgId, onDone, onFail) {
  setPb(pbId, true);
  const iv = setInterval(async () => {
    try {
      const res = await fetch('/api/job/' + jobId);
      const d   = await res.json();
      if (d.state === 'done') {
        clearInterval(iv);
        setPb(pbId, false);
        document.getElementById(pbId).style.width = '100%';
        onDone(d.data);
      } else if (d.state === 'error') {
        clearInterval(iv);
        setPb(pbId, false);
        document.getElementById(pbId).style.width = '0%';
        setMsg(msgId, 'err', '✗ ' + (d.error || 'Unknown error'));
        if (onFail) onFail();
      }
    } catch(e) {
      clearInterval(iv);
      setPb(pbId, false);
      setMsg(msgId, 'err', '✗ Network error');
    }
  }, 900);
}

async function startDownload() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) return;

  currentFilename = null;
  currentClipFilename = null;
  currentSubtitle = '';
  currentFileSize = null;

  document.getElementById('dl-btn').disabled = true;
  document.getElementById('cut-btn').disabled = true;
  document.getElementById('dl-info').style.display = 'none';
  document.getElementById('cut-info').style.display = 'none';
  document.getElementById('subtitle-area').style.display = 'none';
  document.getElementById('dl-pb').style.width = '0%';
  setMsg('dl-msg', '', 'Downloading video and subtitles...');

  try {
    const res = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();

    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      currentFilename = d.filename;
      currentSubtitle = d.subtitle_text || '';
      currentFileSize = d.size_mb || 0;
      setMsg('dl-msg', 'ok', `✓ Ready — ${d.filename} (${formatBytes(currentFileSize)})`);
      document.getElementById('dl-title').textContent = d.title;
      document.getElementById('dl-meta').textContent = 'Duration: ' + d.duration_str + '  ·  Size: ' + formatBytes(currentFileSize);
      document.getElementById('dl-info').style.display = '';
      document.getElementById('dl-full-btn').disabled = false;
      document.getElementById('cut-btn').disabled = false;
      document.getElementById('dl-btn').disabled = false;

      if (currentSubtitle) {
        document.getElementById('subtitle-area').style.display = '';
        document.getElementById('subtitle-text').value = currentSubtitle;
      }
      loadDownloadsList();
    }, () => {
      document.getElementById('dl-btn').disabled = false;
    });
  } catch(e) {
    document.getElementById('dl-btn').disabled = false;
    setMsg('dl-msg', 'err', '✗ Request failed');
  }
}

async function fetchSubtitlesOnly() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) { setMsg('dl-msg', 'err', '✗ Enter a YouTube URL'); return; }

  document.getElementById('dl-btn').disabled = true;
  document.getElementById('cut-btn').disabled = true;
  document.getElementById('dl-info').style.display = 'none';
  document.getElementById('cut-info').style.display = 'none';
  document.getElementById('subtitle-area').style.display = 'none';
  document.getElementById('dl-pb').style.width = '0%';
  setMsg('dl-msg', '', 'Fetching subtitles only...');

  try {
    const res = await fetch('/api/subtitles-only', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();

    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      currentSubtitle = d.subtitle_text || '';
      setMsg('dl-msg', 'ok', `✓ Subtitles ready: ${d.title}`);
      document.getElementById('dl-title').textContent = d.title;
      document.getElementById('dl-meta').textContent = 'Subtitles only (no video)';
      document.getElementById('dl-info').style.display = '';
      document.getElementById('dl-full-btn').disabled = true;
      document.getElementById('cut-btn').disabled = true;
      document.getElementById('dl-btn').disabled = false;

      if (currentSubtitle) {
        document.getElementById('subtitle-area').style.display = '';
        document.getElementById('subtitle-text').value = currentSubtitle;
      } else {
        document.getElementById('subtitle-area').style.display = '';
        document.getElementById('subtitle-text').value = 'No English subtitles found.';
      }
    }, () => {
      document.getElementById('dl-btn').disabled = false;
    });
  } catch(e) {
    document.getElementById('dl-btn').disabled = false;
    setMsg('dl-msg', 'err', '✗ Request failed');
  }
}

async function fetchAndCopySegments() {
  const url = document.getElementById('url-input').value.trim();
  if (!url) { setMsg('dl-msg', 'err', '✗ Enter a YouTube URL'); return; }

  setMsg('dl-msg', '', 'Fetching subtitle segments...');
  try {
    const res = await fetch('/api/subtitles-segments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();

    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      const segmentsJson = JSON.stringify(d.segments, null, 2);
      navigator.clipboard.writeText(segmentsJson);
      setMsg('dl-msg', 'ok', `✓ Segments copied to clipboard! (${d.segments.length} segments)`);
      document.getElementById('subtitle-text').value = segmentsJson;
    }, () => {
      setMsg('dl-msg', 'err', '✗ Failed to fetch segments');
    });
  } catch(e) {
    setMsg('dl-msg', 'err', '✗ Request failed');
  }
}

async function startCut() {
  if (!currentFilename) { setMsg('cut-msg', 'err', '✗ Download a video first'); return; }

  const from = document.getElementById('ts-from').value.trim();
  const to   = document.getElementById('ts-to').value.trim();
  if (!from || !to) { setMsg('cut-msg', 'err', '✗ Both timestamps required'); return; }

  // Validate format (simple regex)
  const tsPattern = /^\d{1,2}:\d{1,2}:\d{2}(?:\.\d{1,3})?$/;
  if (!tsPattern.test(from)) { setMsg('cut-msg', 'err', '✗ Invalid start timestamp (use HH:MM:SS.mmm)'); return; }
  if (!tsPattern.test(to))   { setMsg('cut-msg', 'err', '✗ Invalid end timestamp (use HH:MM:SS.mmm)'); return; }

  currentClipFilename = null;
  document.getElementById('cut-btn').disabled = true;
  document.getElementById('cut-info').style.display = 'none';
  document.getElementById('cut-pb').style.width = '0%';
  setMsg('cut-msg', '', 'Cutting clip...');

  try {
    const cutMode = document.querySelector('input[name="cut-mode"]:checked')?.value || 'normal';
    const res = await fetch('/api/cut', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_filename: currentFilename, ts_from: from, ts_to: to, mode: cutMode })
    });
    const data = await res.json();

    poll(data.job_id, 'cut-pb', 'cut-msg', (d) => {
      currentClipFilename = d.clip_filename;
      setMsg('cut-msg', 'ok', `✓ Clip ready — ${d.from} → ${d.to}`);
      document.getElementById('cut-title').textContent = d.clip_filename;
      document.getElementById('cut-info').style.display = '';
      document.getElementById('cut-dl-btn').disabled = false;
      document.getElementById('cut-btn').disabled = false;
      downloadClip();
    }, () => {
      document.getElementById('cut-btn').disabled = false;
    });
  } catch(e) {
    document.getElementById('cut-btn').disabled = false;
    setMsg('cut-msg', 'err', '✗ Request failed');
  }
}

function downloadFull() {
  if (currentFilename) {
    window.location.href = '/api/download-file/video/' + encodeURIComponent(currentFilename);
  }
}

function downloadClip() {
  if (currentClipFilename) {
    window.location.href = '/api/download-file/clip/' + encodeURIComponent(currentClipFilename);
  }
}

function copySubtitle() {
  const textarea = document.getElementById('subtitle-text');
  textarea.select();
  document.execCommand('copy');
  const btn = document.querySelector('.copy-btn');
  const orig = btn.textContent;
  btn.textContent = '✓ Copied!';
  setTimeout(() => { btn.textContent = orig; }, 1500);
}

async function deleteFile(filename) {
  if (!confirm(`Delete "${filename}"?`)) return;
  try {
    const res = await fetch('/api/downloads/' + encodeURIComponent(filename), { method: 'DELETE' });
    if (res.ok) {
      loadDownloadsList();
      if (currentFilename === filename) {
        currentFilename = null;
        currentClipFilename = null;
        document.getElementById('dl-info').style.display = 'none';
        document.getElementById('cut-btn').disabled = true;
      }
      setMsg('dl-msg', 'ok', `✓ Deleted ${filename}`);
    } else {
      const err = await res.json();
      setMsg('dl-msg', 'err', `✗ Delete failed: ${err.detail}`);
    }
  } catch(e) {
    setMsg('dl-msg', 'err', '✗ Delete request failed');
  }
}

async function deleteClip(filename) {
  if (!confirm(`Delete "${filename}"?`)) return;
  try {
    const res = await fetch('/api/clips/' + encodeURIComponent(filename), { method: 'DELETE' });
    if (res.ok) {
      loadDownloadsList();
    } else {
      const err = await res.json();
      alert(`Delete failed: ${err.detail}`);
    }
  } catch(e) {
    alert('Delete request failed');
  }
}


async function loadDownloadsList() {
  const videoContainer = document.getElementById('downloads-list');
  const clipContainer  = document.getElementById('clips-list');
  videoContainer.innerHTML = '<div class="msg">Loading...</div>';
  clipContainer.innerHTML  = '<div class="msg">Loading...</div>';
  try {
    const res  = await fetch('/api/downloads/list');
    const data = await res.json();

    // Videos
    if (!data.videos.length) {
      videoContainer.innerHTML = '<div class="msg">No downloaded videos yet.</div>';
    } else {
      videoContainer.innerHTML = data.videos.map(f => `
        <div class="file-item">
          <div class="file-info">
            <div class="file-name">${escapeHtml(f.filename)}</div>
            <div class="file-meta">${f.size_mb} MB · ${f.modified}</div>
          </div>
          <div class="file-actions">
            <a href="/api/download-file/video/${encodeURIComponent(f.filename)}" class="sec" style="padding:5px 12px;border-radius:6px;text-decoration:none;color:var(--text);border:1px solid var(--border);">⬇ Download</a>
            <button class="delete-btn" onclick="deleteFile('${escapeHtml(f.filename)}')">🗑 Delete</button>
          </div>
        </div>
      `).join('');
    }

    // Clips
    if (!data.clips.length) {
      clipContainer.innerHTML = '<div class="msg">No clips yet.</div>';
    } else {
      clipContainer.innerHTML = data.clips.map(f => `
        <div class="file-item">
          <div class="file-info">
            <div class="file-name">${escapeHtml(f.filename)}</div>
            <div class="file-meta">${f.size_mb} MB · ${f.modified}</div>
          </div>
          <div class="file-actions">
            <a href="/api/download-file/clip/${encodeURIComponent(f.filename)}" class="sec" style="padding:5px 12px;border-radius:6px;text-decoration:none;color:var(--text);border:1px solid var(--border);">⬇ Download</a>
            <button class="delete-btn" onclick="deleteClip('${escapeHtml(f.filename)}')">🗑 Delete</button>
          </div>
        </div>
      `).join('');
    }
  } catch(e) {
    videoContainer.innerHTML = '<div class="msg err">Failed to load.</div>';
    clipContainer.innerHTML  = '<div class="msg err">Failed to load.</div>';
  }
}


function escapeHtml(str) {
  return str.replace(/[&<>]/g, function(m) {
    if (m === '&') return '&amp;';
    if (m === '<') return '&lt;';
    if (m === '>') return '&gt;';
    return m;
  });
}

document.getElementById('url-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') startDownload();
});

loadDownloadsList();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# PYDANTIC MODELS
# ---------------------------------------------------------------------------
class DownloadRequest(BaseModel):
    url: str

class CutRequest(BaseModel):
    source_filename: str
    ts_from: str
    ts_to: str
    mode: str = "normal"  # "normal" | "9:16"

    @validator('ts_from', 'ts_to')
    def validate_timestamp(cls, v):
        if not validate_timestamp(v):
            raise ValueError(f"Invalid timestamp format: {v}. Use HH:MM:SS.mmm (e.g., 01:34:33.000)")
        return v

class SubtitleOnlyRequest(BaseModel):
    url: str

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML)

@app.post("/api/download")
async def api_download(req: DownloadRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=download_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/subtitles-only")
async def api_subtitles_only(req: SubtitleOnlyRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(target=subtitle_only_worker, args=(job_id, req.url), daemon=True).start()
    return {"job_id": job_id}

@app.post("/api/subtitles-segments")
async def api_subtitles_segments(req: SubtitleOnlyRequest):
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
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        })

    clips = []
    for f in sorted(CLIPS.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True):
        stat = f.stat()
        clips.append({
            "filename": f.name,
            "size_mb": round(stat.st_size / (1024 * 1024), 2),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        })

    return {"videos": videos, "clips": clips}
    
@app.delete("/api/clips/{filename}")
async def delete_clip(filename: str):
    path = CLIPS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    try:
        path.unlink()
        return {"message": f"Deleted {filename}"}
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {str(e)}")

@app.delete("/api/downloads/{filename}")
async def delete_download(filename: str):
    path = DOWNLOADS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    try:
        path.unlink()
        return {"message": f"Deleted {filename}"}
    except Exception as e:
        raise HTTPException(500, f"Delete failed: {str(e)}")

@app.get("/api/download-file/video/{filename}")
async def download_video(filename: str):
    path = DOWNLOADS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(
        str(path),
        media_type="video/mp4",
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
    )

@app.get("/api/download-file/clip/{filename}")
async def download_clip(filename: str):
    path = CLIPS / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(
        str(path),
        media_type="video/mp4",
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"}
    )
