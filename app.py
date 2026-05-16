# app.py — yt-dlp FastAPI server (v5.0.1, >= 2026.03.17 compliant)

import os
import asyncio
import threading
import time
import shutil
import uuid
from pathlib import Path
from typing import Optional, List, Dict

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

# ── Setup ─────────────────────────────────────────────

app = FastAPI(title="yt-dlp server", version="5.0.1")

DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "./downloads"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
print("FFMPEG PATH:", shutil.which("ffmpeg"))
CLEANUP_AFTER_MINUTES = int(os.environ.get("CLEANUP_AFTER_MINUTES", 10))
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "./cookies.txt"))

progress_store: Dict[str, dict] = {}
VERBOSE = os.environ.get("YTDLP_VERBOSE", "0") == "1"

BASE_OPTS = {
    "quiet": False,
    "no_warnings": not VERBOSE,
    "noplaylist": True,
    "socket_timeout": 30,
    "retries": 3,
    "fragment_retries": 3,
    "concurrent_fragment_downloads": 3,
    "nocheckcertificate": True,
    # 2026 JS runtime — 'path': None lets yt-dlp auto-detect the deno binary on PATH
    "js_runtimes": {
        "deno": {"path": None},
    },
    "extractor_args": {
        "youtube": {
            # web/android clients honour cookiefile; ios does not — keep clients cookie-compatible
            "player_client": ["web", "android"],
            "remote_components": ["ejs:github"],
        },
    },
    # Eagerly attach cookies if the file exists at startup.
    # 'cookies' is the correct Python API key (≥2026.03.17); 'cookiefile' is legacy CLI-only.
    **( {"cookies": str(COOKIES_FILE)} if COOKIES_FILE.exists() else {} ),
}

# ── Models ────────────────────────────────────────────

class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None

# ── Format Normalizer ─────────────────────────────────

def normalize_selector(format_id: Optional[str]) -> str:
    if format_id:
        return f"bestvideo[format_id={format_id}]+bestaudio"
    return "b"


def simplify_formats(formats: List[dict]) -> dict:
    video_only = []
    video_audio = []
    audio_only = []

    for f in formats:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        ext = f.get("ext", "")
        height = f.get("height")        # None for audio-only

        if vcodec == "none" and acodec == "none":
            continue

        # Video formats: skip if resolution is below 360p
        if vcodec != "none":
            if height is None or height < 360:
                continue

        entry = {
            "format_id": f["format_id"],
            "ext": ext,
            "resolution": f.get("resolution") or (f"{height}p" if height else ""),
            "vcodec": vcodec if vcodec != "none" else None,
            "acodec": acodec if acodec != "none" else None,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "note": f.get("format_note", ""),
        }

        if vcodec != "none" and acodec == "none":
            if ext == "mp4":
                video_only.append(entry)
        elif vcodec != "none" and acodec != "none":
            if ext == "mp4":
                video_audio.append(entry)
        elif vcodec == "none" and acodec != "none":
            if ext == "m4a":       # ← changed from "mp3" to "m4a"
                audio_only.append(entry)

    return {
        "video_only": video_only,
        "video_audio": video_audio,
        "audio_only": audio_only,
    }

# ── Core Logic ────────────────────────────────────────

def fetch_info(url: str):
    opts = {**BASE_OPTS, "skip_download": True}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        formats = info.get("formats", [])
        simplified = simplify_formats(formats)

        return {
            "title": info.get("title"),
            "duration": info.get("duration"),
            "formats": simplified,
        }
    except Exception as e:
        print(f"[ERROR] fetch_info failed: {str(e)}")
        raise e

def download_video(url: str, download_id: str, format_id: Optional[str]):
    selector = normalize_selector(format_id)
    output_template = str(DOWNLOADS_DIR / f"%(title)s-{download_id[:8]}.%(ext)s")

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0
            progress_store[download_id].update({
                "status": "downloading",
                "progress": round(percent, 2),
                "text": f"{round(percent,2)}%",
            })
        elif d["status"] == "finished":
            progress_store[download_id].update({
                "status": "processing",
                "progress": 95,
                "text": "processing...",
            })

    ydl_opts = {
        **BASE_OPTS,
        "format": selector,
        "outtmpl": output_template,
        "progress_hooks": [hook],
        "merge_output_format": "mp4",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)

        file = Path(filepath)
        progress_store[download_id].update({
            "status": "completed",
            "progress": 100,
            "text": "done",
            "title": info.get("title"),
            "filename": file.name,
            "filepath": str(file),
        })
        threading.Thread(target=cleanup_file, args=(file,), daemon=True).start()

    except Exception as e:
        print(f"[ERROR] download_video failed for {download_id}: {str(e)}")
        progress_store[download_id].update({
            "status": "error",
            "error": str(e),
        })

def cleanup_file(file: Path):
    time.sleep(CLEANUP_AFTER_MINUTES * 60)
    try:
        if file.exists():
            file.unlink()
    except:
        pass

# ── API & UI ──────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home():
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>EXTRACT // yt-dlp</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Mono:ital,wght@0,400;0,700;1,400&family=Bebas+Neue&family=DM+Sans:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #080c0f;
    --bg2:       #0d1318;
    --bg3:       #111920;
    --border:    #1c2a35;
    --border2:   #243342;
    --amber:     #f59e0b;
    --amber-dim: #92610a;
    --green:     #22d3a0;
    --green-dim: #0e6b52;
    --red:       #ef4444;
    --muted:     #3d5566;
    --text:      #c8dde8;
    --text-dim:  #4a6272;
    --white:     #eef6fb;
    --mono:      'Space Mono', monospace;
    --display:   'Bebas Neue', sans-serif;
    --body:      'DM Sans', sans-serif;
  }

  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: var(--body);
    overflow-x: hidden;
  }

  /* ── Scanline overlay ── */
  body::before {
    content: '';
    position: fixed;
    inset: 0;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0,0,0,0.08) 2px,
      rgba(0,0,0,0.08) 4px
    );
    pointer-events: none;
    z-index: 9999;
  }

  /* ── Ambient glow blobs ── */
  .glow-blob {
    position: fixed;
    border-radius: 50%;
    filter: blur(120px);
    pointer-events: none;
    z-index: 0;
    opacity: 0.07;
  }
  .glow-blob.amber { width: 500px; height: 500px; background: var(--amber); top: -150px; right: -150px; }
  .glow-blob.green { width: 400px; height: 400px; background: var(--green); bottom: -100px; left: -100px; }

  /* ── Layout ── */
  .shell {
    position: relative;
    z-index: 1;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 48px 20px 80px;
  }

  /* ── Header ── */
  .header {
    width: 100%;
    max-width: 680px;
    margin-bottom: 52px;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .header-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .wordmark {
    font-family: var(--display);
    font-size: 54px;
    letter-spacing: 0.06em;
    color: var(--white);
    line-height: 1;
  }
  .wordmark span { color: var(--amber); }

  .version-badge {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--amber);
    border: 1px solid var(--amber-dim);
    padding: 3px 8px;
    border-radius: 2px;
    background: rgba(245,158,11,0.06);
    letter-spacing: 0.1em;
  }

  .header-sub {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--muted);
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }

  .divider {
    width: 100%;
    height: 1px;
    background: linear-gradient(90deg, var(--amber) 0%, var(--border2) 40%, transparent 100%);
    margin-top: 16px;
  }

  /* ── Card ── */
  .card {
    width: 100%;
    max-width: 680px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
    position: relative;
  }

  .card::before {
    content: '';
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 2px;
    background: linear-gradient(90deg, var(--amber), transparent 60%);
  }

  .card-header {
    padding: 14px 20px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    gap: 10px;
    background: var(--bg3);
  }

  .dot { width: 7px; height: 7px; border-radius: 50%; }
  .dot.red   { background: #ef4444; box-shadow: 0 0 6px #ef4444; }
  .dot.amber { background: var(--amber); box-shadow: 0 0 6px var(--amber); }
  .dot.green { background: var(--green); box-shadow: 0 0 6px var(--green); }

  .card-label {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text-dim);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    margin-left: auto;
  }

  .card-body { padding: 24px 20px; }

  /* ── URL Input ── */
  .input-group {
    display: flex;
    gap: 0;
    border: 1px solid var(--border2);
    border-radius: 3px;
    overflow: hidden;
    transition: border-color 0.2s;
  }
  .input-group:focus-within { border-color: var(--amber); }

  .input-prefix {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--amber);
    background: rgba(245,158,11,0.07);
    border-right: 1px solid var(--border2);
    padding: 0 14px;
    display: flex;
    align-items: center;
    white-space: nowrap;
    letter-spacing: 0.06em;
    user-select: none;
  }

  #url {
    flex: 1;
    background: transparent;
    border: none;
    outline: none;
    color: var(--white);
    font-family: var(--mono);
    font-size: 12px;
    padding: 13px 16px;
    letter-spacing: 0.03em;
  }
  #url::placeholder { color: var(--text-dim); }

  /* ── Buttons ── */
  .btn {
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border: none;
    cursor: pointer;
    border-radius: 3px;
    padding: 12px 22px;
    transition: all 0.15s;
    position: relative;
    overflow: hidden;
  }

  .btn::after {
    content: '';
    position: absolute;
    inset: 0;
    background: white;
    opacity: 0;
    transition: opacity 0.15s;
  }
  .btn:active::after { opacity: 0.07; }

  .btn-amber {
    background: var(--amber);
    color: #000;
  }
  .btn-amber:hover { background: #fbbf24; }
  .btn-amber:disabled { background: var(--amber-dim); color: #4a3300; cursor: not-allowed; }

  .btn-outline {
    background: transparent;
    color: var(--green);
    border: 1px solid var(--green-dim);
  }
  .btn-outline:hover { background: rgba(34,211,160,0.08); border-color: var(--green); }
  .btn-outline:disabled { color: var(--muted); border-color: var(--border); cursor: not-allowed; }

  .btn-ghost {
    background: transparent;
    color: var(--text-dim);
    border: 1px solid var(--border2);
  }
  .btn-ghost:hover { color: var(--text); border-color: var(--muted); }

  .actions { display: flex; gap: 10px; margin-top: 14px; }

  /* ── Status line ── */
  .status-line {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text-dim);
    margin-top: 10px;
    min-height: 16px;
    letter-spacing: 0.06em;
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .status-line .pulse {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--amber);
    box-shadow: 0 0 6px var(--amber);
    animation: pulse 1s infinite;
    flex-shrink: 0;
  }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }

  /* ── Formats panel ── */
  .formats-panel {
    margin-top: 20px;
    border: 1px solid var(--border);
    border-radius: 3px;
    overflow: hidden;
    display: none;
  }
  .formats-panel.visible { display: block; }

  .formats-header {
    padding: 10px 16px;
    background: var(--bg3);
    border-bottom: 1px solid var(--border);
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.1em;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .format-section { border-bottom: 1px solid var(--border); }
  .format-section:last-child { border-bottom: none; }

  .format-section-label {
    padding: 8px 16px;
    font-family: var(--mono);
    font-size: 9px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    display: flex;
    align-items: center;
    gap: 8px;
    background: rgba(255,255,255,0.01);
  }
  .format-section-label.vid-only { color: var(--amber); }
  .format-section-label.vid-audio { color: var(--green); }
  .format-section-label.aud-only { color: #a78bfa; }

  .format-row {
    display: flex;
    align-items: center;
    padding: 9px 16px;
    gap: 12px;
    cursor: pointer;
    transition: background 0.12s;
    border-top: 1px solid transparent;
    user-select: none;
  }
  .format-row:hover { background: rgba(255,255,255,0.03); }
  .format-row.selected {
    background: rgba(245,158,11,0.07);
    border-top-color: transparent;
    border-left: 2px solid var(--amber);
  }

  .format-radio {
    width: 12px; height: 12px;
    border-radius: 50%;
    border: 1px solid var(--muted);
    flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    transition: all 0.12s;
  }
  .format-row.selected .format-radio {
    border-color: var(--amber);
    background: var(--amber);
  }
  .format-radio-dot {
    width: 4px; height: 4px;
    border-radius: 50%;
    background: #000;
    opacity: 0;
    transition: opacity 0.12s;
  }
  .format-row.selected .format-radio-dot { opacity: 1; }

  .format-res {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--white);
    min-width: 56px;
    font-weight: 700;
  }

  .format-codec {
    font-family: var(--mono);
    font-size: 9px;
    color: var(--text-dim);
    background: var(--bg3);
    border: 1px solid var(--border);
    padding: 2px 6px;
    border-radius: 2px;
    letter-spacing: 0.06em;
  }

  .format-size {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--muted);
    margin-left: auto;
  }

  .format-note {
    font-family: var(--mono);
    font-size: 9px;
    color: var(--text-dim);
    letter-spacing: 0.04em;
  }

  /* ── Meta panel ── */
  .meta-panel {
    display: none;
    margin-top: 0;
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    background: var(--bg3);
    gap: 24px;
    flex-wrap: wrap;
  }
  .meta-panel.visible { display: flex; }

  .meta-item { display: flex; flex-direction: column; gap: 2px; }
  .meta-key {
    font-family: var(--mono);
    font-size: 9px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }
  .meta-val {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--white);
  }

  /* ── Progress card ── */
  .progress-card {
    width: 100%;
    max-width: 680px;
    margin-top: 16px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 4px;
    overflow: hidden;
    display: none;
  }
  .progress-card.visible { display: block; }
  .progress-card::before {
    content: '';
    display: block;
    height: 2px;
    background: linear-gradient(90deg, var(--green), transparent 80%);
  }

  .progress-inner { padding: 20px; }

  .progress-title {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--text-dim);
    letter-spacing: 0.1em;
    text-transform: uppercase;
    margin-bottom: 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .progress-filename {
    font-family: var(--mono);
    font-size: 11px;
    color: var(--text);
    margin-bottom: 14px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  .track {
    width: 100%;
    height: 3px;
    background: var(--border2);
    border-radius: 0;
    overflow: visible;
    position: relative;
  }

  .fill {
    height: 100%;
    width: 0%;
    background: linear-gradient(90deg, var(--green), #6ee7c7);
    border-radius: 0;
    transition: width 0.4s cubic-bezier(0.4,0,0.2,1);
    position: relative;
  }

  .fill::after {
    content: '';
    position: absolute;
    right: -1px;
    top: -3px;
    width: 3px;
    height: 9px;
    background: var(--green);
    box-shadow: 0 0 10px var(--green);
    border-radius: 1px;
  }

  .progress-meta {
    display: flex;
    justify-content: space-between;
    margin-top: 12px;
  }

  .progress-pct {
    font-family: var(--display);
    font-size: 36px;
    color: var(--green);
    letter-spacing: 0.04em;
    line-height: 1;
  }

  .progress-status {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--muted);
    text-align: right;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    gap: 2px;
  }

  /* ── Error state ── */
  .error-panel {
    margin-top: 14px;
    padding: 14px 16px;
    background: rgba(239,68,68,0.06);
    border: 1px solid rgba(239,68,68,0.25);
    border-radius: 3px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--red);
    display: none;
    line-height: 1.6;
    letter-spacing: 0.02em;
  }
  .error-panel.visible { display: block; }

  /* ── Download complete ── */
  .complete-panel {
    display: none;
    flex-direction: column;
    align-items: center;
    padding: 28px 20px;
    gap: 14px;
    text-align: center;
  }
  .complete-panel.visible { display: flex; }

  .complete-icon {
    width: 52px; height: 52px;
    border-radius: 50%;
    border: 2px solid var(--green);
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 0 24px rgba(34,211,160,0.2);
    animation: popIn 0.4s cubic-bezier(0.34,1.56,0.64,1);
  }
  @keyframes popIn { from{transform:scale(0.5);opacity:0} to{transform:scale(1);opacity:1} }

  .complete-icon svg { stroke: var(--green); }

  .complete-title {
    font-family: var(--display);
    font-size: 28px;
    color: var(--green);
    letter-spacing: 0.08em;
  }

  .complete-subtitle {
    font-family: var(--mono);
    font-size: 10px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.1em;
  }

  /* ── Footer ── */
  .footer {
    margin-top: 40px;
    font-family: var(--mono);
    font-size: 9px;
    color: var(--text-dim);
    letter-spacing: 0.12em;
    text-transform: uppercase;
    display: flex;
    gap: 24px;
    opacity: 0.5;
  }

  /* ── Animations ── */
  @keyframes fadeSlide {
    from { opacity:0; transform: translateY(8px); }
    to   { opacity:1; transform: translateY(0); }
  }
  .anim-in { animation: fadeSlide 0.3s ease forwards; }

  /* ── Spinning loader ── */
  .spinner {
    display: inline-block;
    width: 10px; height: 10px;
    border: 1.5px solid var(--border2);
    border-top-color: var(--amber);
    border-radius: 50%;
    animation: spin 0.7s linear infinite;
    flex-shrink: 0;
  }
  @keyframes spin { to{transform:rotate(360deg)} }
</style>
</head>
<body>
<div class="glow-blob amber"></div>
<div class="glow-blob green"></div>

<div class="shell">

  <!-- Header -->
  <header class="header">
    <div class="header-top">
      <div class="wordmark">EXTR<span>A</span>CT</div>
      <div class="version-badge">v5.0.1 // yt-dlp</div>
    </div>
    <div class="header-sub">Media acquisition system // YouTube pipeline</div>
    <div class="divider"></div>
  </header>

  <!-- Main card -->
  <div class="card anim-in">
    <div class="card-header">
      <div class="dot red"></div>
      <div class="dot amber"></div>
      <div class="dot green"></div>
      <div class="card-label">Input // URL resolver</div>
    </div>
    <div class="card-body">

      <div class="input-group">
        <div class="input-prefix">YT://</div>
        <input id="url" type="text" placeholder="paste target url…" autocomplete="off" spellcheck="false" />
      </div>

      <div class="actions">
        <button class="btn btn-amber" id="btn-info" onclick="getInfo()">Resolve</button>
        <button class="btn btn-outline" id="btn-dl" onclick="start()" disabled>Extract</button>
        <button class="btn btn-ghost" onclick="reset()">Reset</button>
      </div>

      <div class="status-line" id="status-line"></div>

      <!-- Meta -->
      <div class="meta-panel" id="meta-panel">
        <div class="meta-item">
          <div class="meta-key">Title</div>
          <div class="meta-val" id="meta-title">—</div>
        </div>
        <div class="meta-item">
          <div class="meta-key">Duration</div>
          <div class="meta-val" id="meta-duration">—</div>
        </div>
      </div>

      <!-- Formats -->
      <div class="formats-panel" id="formats-panel">
        <div class="formats-header">
          <span>Select stream</span>
          <span id="format-count"></span>
        </div>
        <div id="formats-list"></div>
      </div>

      <!-- Error -->
      <div class="error-panel" id="error-panel"></div>

    </div>
  </div>

  <!-- Progress card -->
  <div class="progress-card" id="progress-card">
    <div class="progress-inner" id="progress-inner">
      <div class="progress-title">
        <span>Extraction progress</span>
        <span id="prog-id" style="color:var(--text-dim);font-size:9px"></span>
      </div>
      <div class="progress-filename" id="prog-filename"></div>
      <div class="track"><div class="fill" id="fill"></div></div>
      <div class="progress-meta">
        <div class="progress-pct" id="prog-pct">0%</div>
        <div class="progress-status" id="prog-status"></div>
      </div>
    </div>

    <div class="complete-panel" id="complete-panel">
      <div class="complete-icon">
        <svg width="24" height="24" fill="none" stroke-width="2.5" viewBox="0 0 24 24">
          <polyline points="20 6 9 17 4 12"/>
        </svg>
      </div>
      <div class="complete-title">Acquired</div>
      <div class="complete-subtitle" id="complete-filename"></div>
      <button class="btn btn-outline" id="btn-save" onclick="">Save file</button>
    </div>
  </div>

  <footer class="footer">
    <span>yt-dlp pipeline</span>
    <span>ffmpeg muxer</span>
    <span>auto-cleanup: 10m</span>
  </footer>

</div>

<script>
  let currentId = null;
  let selectedFormatId = null;
  let pollTimer = null;

  function fmtDuration(s) {
    if (!s) return '—';
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = s%60;
    return h ? `${h}h ${m}m ${sec}s` : `${m}m ${sec}s`;
  }

  function fmtSize(bytes) {
    if (!bytes) return '?';
    return (bytes / 1_000_000).toFixed(1) + ' MB';
  }

  function setStatus(msg, loading=false) {
    const el = document.getElementById('status-line');
    if (!msg) { el.innerHTML = ''; return; }
    el.innerHTML = loading
      ? `<span class="spinner"></span><span>${msg}</span>`
      : `<span>${msg}</span>`;
  }

  function showError(msg) {
    const el = document.getElementById('error-panel');
    el.textContent = '// ERROR: ' + msg;
    el.classList.add('visible');
  }

  function clearError() {
    document.getElementById('error-panel').classList.remove('visible');
  }

  function reset() {
    clearInterval(pollTimer);
    currentId = null;
    selectedFormatId = null;
    document.getElementById('url').value = '';
    document.getElementById('formats-panel').classList.remove('visible');
    document.getElementById('meta-panel').classList.remove('visible');
    document.getElementById('progress-card').classList.remove('visible');
    document.getElementById('formats-list').innerHTML = '';
    document.getElementById('btn-dl').disabled = true;
    clearError();
    setStatus('');
  }

  function selectFormat(id, el) {
    document.querySelectorAll('.format-row').forEach(r => r.classList.remove('selected'));
    el.classList.add('selected');
    selectedFormatId = id;
    document.getElementById('btn-dl').disabled = false;
  }

  async function getInfo() {
    const url = document.getElementById('url').value.trim();
    if (!url) { showError('No URL provided.'); return; }

    clearError();
    setStatus('Resolving stream metadata…', true);
    document.getElementById('btn-info').disabled = true;
    document.getElementById('formats-panel').classList.remove('visible');
    document.getElementById('meta-panel').classList.remove('visible');
    document.getElementById('btn-dl').disabled = true;
    selectedFormatId = null;

    try {
      const res = await fetch('/info', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url })
      });
      if (!res.ok) throw new Error((await res.json()).detail || res.statusText);
      const data = await res.json();

      // Meta
      document.getElementById('meta-title').textContent = data.title || '—';
      document.getElementById('meta-duration').textContent = fmtDuration(data.duration);
      document.getElementById('meta-panel').classList.add('visible');

      // Formats
      const list = document.getElementById('formats-list');
      list.innerHTML = '';

      const sections = [
        { key: 'video_only',  label: '▶ Video only',       cls: 'vid-only',  emoji: '' },
        { key: 'video_audio', label: '▶ Video + Audio',    cls: 'vid-audio', emoji: '' },
        { key: 'audio_only',  label: '♪ Audio only',       cls: 'aud-only',  emoji: '' },
      ];

      let totalCount = 0;

      sections.forEach(({ key, label, cls }) => {
        const items = data.formats[key] || [];
        if (!items.length) return;
        totalCount += items.length;

        const sec = document.createElement('div');
        sec.className = 'format-section';

        const hdr = document.createElement('div');
        hdr.className = `format-section-label ${cls}`;
        hdr.textContent = label;
        sec.appendChild(hdr);

        items.forEach(f => {
          const row = document.createElement('div');
          row.className = 'format-row';
          row.innerHTML = `
            <div class="format-radio"><div class="format-radio-dot"></div></div>
            <div class="format-res">${f.resolution || 'audio'}</div>
            <div class="format-codec">${f.vcodec || f.acodec || f.ext}</div>
            <div class="format-note">${f.note || ''}</div>
            <div class="format-size">${fmtSize(f.filesize)}</div>
          `;
          row.addEventListener('click', () => selectFormat(f.format_id, row));
          sec.appendChild(row);
        });

        list.appendChild(sec);
      });

      document.getElementById('format-count').textContent = totalCount + ' streams';
      document.getElementById('formats-panel').classList.add('visible');
      setStatus('Select a stream and extract →');

    } catch (e) {
      showError(e.message);
      setStatus('');
    } finally {
      document.getElementById('btn-info').disabled = false;
    }
  }

  async function start() {
    if (!selectedFormatId) return;
    const url = document.getElementById('url').value.trim();
    clearError();

    const card = document.getElementById('progress-card');
    card.classList.add('visible');
    document.getElementById('progress-inner').style.display = 'block';
    document.getElementById('complete-panel').classList.remove('visible');
    document.getElementById('fill').style.width = '0%';
    document.getElementById('prog-pct').textContent = '0%';
    document.getElementById('prog-filename').textContent = '';
    setStatus('Initiating extraction…', true);

    const res = await fetch('/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, format_id: selectedFormatId })
    });
    const data = await res.json();
    currentId = data.download_id;
    document.getElementById('prog-id').textContent = currentId.slice(0,8);
    poll();
  }

  async function poll() {
    if (!currentId) return;
    try {
      const res = await fetch('/progress/' + currentId);
      const data = await res.json();

      const pct = data.progress || 0;
      document.getElementById('fill').style.width = pct + '%';
      document.getElementById('prog-pct').textContent = Math.round(pct) + '%';

      const statusMap = {
        starting:    'Initialising…',
        downloading: `Downloading — ${data.text || ''}`,
        processing:  'Muxing streams…',
        completed:   'Done',
        error:       'Error'
      };
      document.getElementById('prog-status').innerHTML =
        `<span>${statusMap[data.status] || data.status}</span>`;

      if (data.title) {
        document.getElementById('prog-filename').textContent = data.title;
      }

      if (data.status === 'completed') {
        setStatus('');
        showComplete(data);
        return;
      }

      if (data.status === 'error') {
        showError(data.error);
        setStatus('');
        return;
      }

      pollTimer = setTimeout(poll, 900);
    } catch (e) {
      pollTimer = setTimeout(poll, 2000);
    }
  }

  function showComplete(data) {
    document.getElementById('progress-inner').style.display = 'none';
    const cp = document.getElementById('complete-panel');
    cp.classList.add('visible');
    document.getElementById('complete-filename').textContent = data.filename || 'file ready';
    const btn = document.getElementById('btn-save');
    btn.onclick = () => { window.location = '/download/' + currentId; };
  }

  // Enter key to resolve
  document.getElementById('url').addEventListener('keydown', e => {
    if (e.key === 'Enter') getInfo();
  });
</script>
</body>
</html>"""

# ── API Endpoints ──────────────────────────────────────

@app.post("/info")
async def info(req: InfoRequest):
    try:
        data = await asyncio.to_thread(fetch_info, req.url)
        return data
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/download")
async def download(req: DownloadRequest):
    download_id = str(uuid.uuid4())
    progress_store[download_id] = {
        "status": "starting",
        "progress": 0,
        "text": "starting...",
    }
    threading.Thread(
        target=download_video,
        args=(req.url, download_id, req.format_id),
        daemon=True
    ).start()
    return {"download_id": download_id}

@app.get("/progress/{download_id}")
async def progress(download_id: str):
    data = progress_store.get(download_id)
    if not data:
        return {"status": "not_found"}
    return data

@app.get("/download/{download_id}")
async def serve(download_id: str):
    data = progress_store.get(download_id)
    if not data or data.get("status") != "completed":
        raise HTTPException(404, "file not ready")
    path = Path(data["filepath"])
    if not path.exists():
        raise HTTPException(404, "file missing")
    return FileResponse(path=str(path), filename=data["filename"])

@app.get("/health")
def health():
    return {"status": "ok"}
