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
    # New 2026 Bypass Settings
    'js_runtimes': {
        'deno': {}
    },
     "extractor_args": {
        "youtube": {
      #      "player_client": ["android", "web"],
            "remote_components": ["ejs:github"],
        },
    },
}

# ── Models ────────────────────────────────────────────

class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = None

# ── Logic Helpers ─────────────────────────────────────

def is_auth_error(exception: Exception) -> bool:
    """Check if the error message indicates a need for cookies."""
    err_msg = str(exception).lower()
    return "sign in" in err_msg or "bot" in err_msg or "confirm your age" in err_msg

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

def fetch_info(url: str, use_cookies: bool = False):
    opts = {**BASE_OPTS, "skip_download": True}
    if use_cookies and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

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
        if not use_cookies and is_auth_error(e) and COOKIES_FILE.exists():
            print(f"[RETRY] Auth/Bot error detected for {url}. Retrying with cookies...")
            return fetch_info(url, use_cookies=True)
        
        print(f"[ERROR] fetch_info failed: {str(e)}")
        raise e

def download_video(url: str, download_id: str, format_id: Optional[str], use_cookies: bool = False):
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
        # "format": "bv*+ba/b",
        "format": selector,
        # "format_sort": [
            # 'vcodec:h264',
           # 'vbr',
           # 'height',
           # 'ext:mp4',
          #  'res:1080',      # Aim for 1080p specifically
           # 'acodec:mp4a'
        #],
        "outtmpl": output_template,
        "progress_hooks": [hook],
        "merge_output_format": "mp4",
    }

    if use_cookies and COOKIES_FILE.exists():
        ydl_opts["cookiefile"] = str(COOKIES_FILE)

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
        if not use_cookies and is_auth_error(e) and COOKIES_FILE.exists():
            print(f"[RETRY] Auth/Bot error detected for {download_id}. Retrying with cookies...")
            return download_video(url, download_id, format_id, use_cookies=True)
        
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
    # Content identical to original app 5.py
    return """
<!DOCTYPE html>
<html>
<head>
<title>Downloader</title>
<style>
body{font-family:sans-serif;background:#0f172a;color:#fff;text-align:center;padding-top:60px}
input,select{width:400px;padding:10px;border-radius:8px;border:none;margin:5px}
button{padding:12px 20px;border:none;background:#3b82f6;color:#fff;border-radius:8px}
.bar{width:400px;height:8px;background:#333;margin:20px auto;border-radius:4px}
.fill{height:100%;width:0;background:#22c55e}
</style>
</head>
<body>
  <h2>yt-dlp advanced downloader</h2>
  <input id="url" placeholder="Paste YouTube URL" /><br />
  <button onclick="getInfo()">Get formats</button><br />

  <select id="formats"></select><br />

  <button onclick="start()">Download</button>

  <div class="bar"><div id="fill" class="fill"></div></div>
  <p id="text"></p>

  <script>
    let currentId = null;

    async function getInfo() {
      const url = document.getElementById("url").value;
      const res = await fetch("/info", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url })
      });
      const response = await res.json();
      const data = response.formats;
      const select = document.getElementById("formats");
      select.innerHTML = "";

      function addOption(group, f, prefix) {
        const opt = document.createElement("option");
        opt.value = f.format_id;
        const sizeMB = f.filesize ? (f.filesize / 1_000_000).toFixed(1) : "?";
        opt.text = `${prefix} ${f.resolution || "audio"} (${sizeMB} MB) [${f.vcodec || f.acodec || "?"}]`;
        group.appendChild(opt);
      }

      if (data.video_only && data.video_only.length) {
        const group = document.createElement("optgroup");
        group.label = "🎬 Video-only (no audio)";
        data.video_only.forEach(f => addOption(group, f, ""));
        select.appendChild(group);
      }

      if (data.video_audio && data.video_audio.length) {
        const group = document.createElement("optgroup");
        group.label = "🎥 Video + Audio (single file)";
        data.video_audio.forEach(f => addOption(group, f, ""));
        select.appendChild(group);
      }

      if (data.audio_only && data.audio_only.length) {
        const group = document.createElement("optgroup");
        group.label = "🎵 Audio-only";
        data.audio_only.forEach(f => addOption(group, f, ""));
        select.appendChild(group);
      }
    }

    async function start() {
      const url = document.getElementById("url").value;
      const format_id = document.getElementById("formats").value;
      const res = await fetch("/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url, format_id })
      });
      const data = await res.json();
      currentId = data.download_id;
      poll();
    }

    async function poll() {
      if (!currentId) return;
      const res = await fetch("/progress/" + currentId);
      const data = await res.json();
      document.getElementById("fill").style.width = data.progress + "%";
      document.getElementById("text").innerText = data.text || data.status;
      if (data.status === "completed") {
        window.location = "/download/" + currentId;
        return;
      }
      if (data.status === "error") {
        document.getElementById("text").innerText = data.error;
        return;
      }
      setTimeout(poll, 1000);
    }
  </script>
</body>
</html>
"""

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
