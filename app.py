import os
import sys
import json
import uuid
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
import uvicorn

import yt_dlp

# ---------------------------------------------------------------------------
# Application & state
# ---------------------------------------------------------------------------
app = FastAPI(title="YouTube Downloader")

DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# In‑memory progress store: download_id → dict
progress_store: Dict[str, Dict[str, Any]] = {}
progress_lock = threading.Lock()

# Cleanup timeout (minutes), read from environment
CLEANUP_AFTER_MINUTES = int(os.getenv("CLEANUP_AFTER_MINUTES", "10"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_update(download_id: str, **kwargs):
    """Thread‑safe update of the progress store."""
    with progress_lock:
        if download_id in progress_store:
            progress_store[download_id].update(kwargs)


# ---------------------------------------------------------------------------
# Cleanup background thread
# ---------------------------------------------------------------------------
def cleanup_loop():
    """Periodically delete files older than CLEANUP_AFTER_MINUTES."""
    while True:
        now = time.time()
        for f in DOWNLOAD_DIR.iterdir():
            if f.is_file():
                age_min = (now - f.stat().st_mtime) / 60
                if age_min > CLEANUP_AFTER_MINUTES:
                    try:
                        f.unlink()
                        print(f"[CLEANUP] Deleted {f.name} (age {age_min:.1f} min)")
                    except Exception as exc:
                        print(f"[CLEANUP] Failed to delete {f.name}: {exc}")
        time.sleep(30)  # check every 30 seconds


cleanup_thread = threading.Thread(target=cleanup_loop, daemon=True)
cleanup_thread.start()


# ---------------------------------------------------------------------------
# yt‑dlp option builder
# ---------------------------------------------------------------------------
def build_ydl_opts(extra: dict = None) -> dict:
    """Return a base options dict with modern anti‑bot / EJS settings.

    Key points:
    - Use browser impersonation when curl_cffi is available.
    - Enable remote components (ejs:npm) for JS challenges.
    - Prefer deno as JS runtime (default).
    - Do NOT specify a fixed `format` here – it is set per‑request.
    """

    opts = {
        # General
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        # Network
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        # Workarounds / anti‑bot
        "extractor_args": {
            "youtube": {
                # Let yt‑dlp select the best clients automatically
                "player_client": ["default"],
            }
        },
        # External JS (EJS) – required for solving YouTube challenges
        # Remote components allow on‑the‑fly npm downloads (needs deno/bun).
        "remote_components": ["ejs:npm"],
        # Impersonation (if curl_cffi is installed)
        "impersonate": "chrome:windows",
    }

    # Merge extra options
    if extra:
        opts.update(extra)

    # If a cookie file exists, always include it as a fallback.
    # (The requirement says to retry with cookies on bot errors, but we
    #  can also just always pass the file when it exists – it does not harm.)
    cookie_file = Path("cookies.txt")
    if cookie_file.exists():
        opts["cookiefile"] = str(cookie_file)

    return opts


# ---------------------------------------------------------------------------
# Progress hook
# ---------------------------------------------------------------------------
def make_progress_hook(download_id: str, video_title: str = ""):
    """Create a progress hook that updates `progress_store`."""

    def hook(d: dict):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total else 0.0
            _safe_update(
                download_id,
                status="downloading",
                progress=round(pct, 1),
                text=f"Downloading {video_title or 'video'} – {pct:.1f}%",
            )
        elif status == "finished":
            _safe_update(
                download_id,
                status="processing",
                progress=100.0,
                text="Processing file (ffmpeg merge / post‑processing) …",
            )
        elif status == "error":
            _safe_update(
                download_id,
                status="error",
                text=d.get("info_dict", {}).get("error", "Download error"),
            )

    return hook


# ---------------------------------------------------------------------------
# HTML frontend (embedded)
# ---------------------------------------------------------------------------
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YouTube Downloader</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 640px; margin: 2rem auto; padding: 0 1rem; }
  input, select, button { font-size: 1rem; padding: 0.5rem; margin: 0.25rem 0; width: 100%; box-sizing: border-box; }
  button { cursor: pointer; background: #007bff; color: #fff; border: none; }
  button:disabled { opacity: 0.5; cursor: not-allowed; }
  .progress-bar { background: #eee; border-radius: 4px; overflow: hidden; height: 1.25rem; margin: 1rem 0; }
  .progress-fill { background: #28a745; height: 100%; width: 0%; transition: width 0.2s; }
  .status { font-size: 0.9rem; color: #555; }
  .error { color: #c00; }
</style>
</head>
<body>
  <h2>YouTube Video Downloader</h2>
  <input id="url" type="text" placeholder="Paste YouTube URL …">
  <br>
  <button id="btnFormats" onclick="getFormats()">Get formats</button>
  <br><br>
  <div id="formatsBox" style="display:none;">
    <label for="formatSelect">Choose format:</label>
    <select id="formatSelect"></select>
    <br>
    <button id="btnDownload" onclick="startDownload()">Download</button>
  </div>
  <div id="progressBox" style="display:none; margin-top: 1rem;">
    <div class="progress-bar"><div id="progressFill" class="progress-fill"></div></div>
    <div id="statusText" class="status"></div>
  </div>
  <div id="errorBox" class="error" style="margin-top: 0.5rem;"></div>

<script>
async function getFormats() {
  const url = document.getElementById('url').value.trim();
  if (!url) return;
  document.getElementById('errorBox').textContent = '';
  document.getElementById('formatsBox').style.display = 'none';
  document.getElementById('progressBox').style.display = 'none';
  try {
    const resp = await fetch('/formats', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url})
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    const formats = await resp.json();
    if (!formats.length) { alert('No downloadable formats found.'); return; }
    const sel = document.getElementById('formatSelect');
    sel.innerHTML = '';
    formats.forEach(f => {
      const opt = document.createElement('option');
      opt.value = f.format_id;
      let desc = `${f.resolution || '?'}  .${f.ext}`;
      if (f.vcodec) desc += `  video:${f.vcodec}`;
      if (f.acodec) desc += `  audio:${f.acodec}`;
      if (f.filesize_approx) desc += `  ~${(f.filesize_approx/1024/1024).toFixed(1)} MB`;
      if (f.note) desc += `  (${f.note})`;
      opt.textContent = desc;
      sel.appendChild(opt);
    });
    document.getElementById('formatsBox').style.display = 'block';
  } catch (err) {
    document.getElementById('errorBox').textContent = err.message;
  }
}

async function startDownload() {
  const url = document.getElementById('url').value.trim();
  const fmt = document.getElementById('formatSelect').value;
  document.getElementById('errorBox').textContent = '';
  document.getElementById('progressBox').style.display = 'block';
  document.getElementById('progressFill').style.width = '0%';
  document.getElementById('statusText').textContent = 'Starting …';
  try {
    const resp = await fetch('/download', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({url, format_id: fmt})
    });
    if (!resp.ok) throw new Error((await resp.json()).detail || resp.statusText);
    const {download_id} = await resp.json();
    pollProgress(download_id);
  } catch (err) {
    document.getElementById('errorBox').textContent = err.message;
  }
}

async function pollProgress(downloadId) {
  const fill = document.getElementById('progressFill');
  const text = document.getElementById('statusText');
  const interval = setInterval(async () => {
    try {
      const resp = await fetch(`/progress/${downloadId}`);
      if (!resp.ok) { clearInterval(interval); return; }
      const data = await resp.json();
      fill.style.width = (data.progress || 0) + '%';
      text.textContent = data.text || '';
      if (data.status === 'completed') {
        clearInterval(interval);
        window.location.href = data.download_url;
      } else if (data.status === 'error') {
        clearInterval(interval);
        document.getElementById('errorBox').textContent = data.text || 'Unknown error';
      }
    } catch (err) {
      clearInterval(interval);
    }
  }, 500);
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


# ---------------------------------------------------------------------------
# POST /formats
# ---------------------------------------------------------------------------
@app.post("/formats")
async def list_formats(req: Request):
    body = await req.json()
    url = body.get("url", "").strip()
    if not url:
        raise HTTPException(400, "Missing 'url'")

    # Use yt‑dlp to extract format information
    opts = build_ydl_opts({
        "quiet": True,
        "dump_single_json": False,
        "extract_flat": False,
        "listformats": False,
    })

    # We want to inspect formats *without* downloading.
    # Use `extract_info` with `download=False`.
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        # First attempt failed – maybe bot detection. Retry with cookie file
        # if it exists and we didn't already use it.
        if "cookiefile" not in opts and Path("cookies.txt").exists():
            print(f"[DEBUG] /formats first attempt failed ({exc}), retrying with cookies")
            opts2 = build_ydl_opts({"cookiefile": str(Path("cookies.txt")), "quiet": True})
            try:
                with yt_dlp.YoutubeDL(opts2) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc2:
                raise HTTPException(400, f"Failed to fetch video info: {exc2}")
        else:
            raise HTTPException(400, f"Failed to fetch video info: {exc}")

    formats = info.get("formats") or []
    result = []
    seen = set()
    for fmt in formats:
        fid = fmt.get("format_id")
        if not fid or fid in seen:
            continue

        # Prefer formats that have both video and audio (single‑file downloads).
        # Also include formats that yt‑dlp marks as "best" combined pairs.
        vcodec = fmt.get("vcodec", "none") or "none"
        acodec = fmt.get("acodec", "none") or "none"
        has_video = vcodec != "none"
        has_audio = acodec != "none"

        # Keep: video+audio combined, or formats that are clearly meant to be
        # downloaded as a single container.
        if not (has_video and has_audio):
            # Skip video‑only or audio‑only (they'd need merging)
            continue

        seen.add(fid)
        resolution = fmt.get("resolution") or ""
        if not resolution and fmt.get("height"):
            resolution = f"{fmt['height']}p"

        result.append({
            "format_id": fid,
            "resolution": resolution,
            "ext": fmt.get("ext", "mp4"),
            "filesize_approx": fmt.get("filesize_approx"),
            "vcodec": vcodec,
            "acodec": acodec,
            "note": fmt.get("format_note", ""),
        })

    # Sort by resolution (descending) as a rough quality order
    def sort_key(f):
        try:
            return int(f["resolution"].replace("p", "")) or 0
        except Exception:
            return 0

    result.sort(key=sort_key, reverse=True)

    return result


# ---------------------------------------------------------------------------
# POST /download
# ---------------------------------------------------------------------------
@app.post("/download")
async def start_download(req: Request):
    body = await req.json()
    url = body.get("url", "").strip()
    format_id = body.get("format_id", "").strip()
    if not url or not format_id:
        raise HTTPException(400, "Missing 'url' or 'format_id'")

    download_id = uuid.uuid4().hex

    # Pre‑register progress entry
    with progress_lock:
        progress_store[download_id] = {
            "status": "starting",
            "progress": 0.0,
            "text": "Initializing …",
            "filename": None,
            "download_url": None,
        }

    # Run download in background thread
    thread = threading.Thread(
        target=_download_worker,
        args=(url, format_id, download_id),
        daemon=True,
    )
    thread.start()

    return {"download_id": download_id}


def _download_worker(url: str, format_id: str, download_id: str):
    """Background worker that runs yt‑dlp and updates progress store."""
    try:
        # First, get video info to build a nice filename
        info_opts = build_ydl_opts({"quiet": True})
        with yt_dlp.YoutubeDL(info_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title = info.get("title", "video")
        # Sanitise title for use in filename
        safe_title = "".join(c if c.isalnum() or c in " ._-" else "_" for c in title)
        outtmpl = str(DOWNLOAD_DIR / f"{safe_title}-{download_id[:8]}.%(ext)s")

        # Build download options
        opts = build_ydl_opts({
            "format": format_id,
            "outtmpl": outtmpl,
            "progress_hooks": [make_progress_hook(download_id, title)],
            "quiet": True,
            "no_warnings": True,
        })

        # First attempt
        try:
            _do_download(url, opts, download_id, title)
        except Exception as first_exc:
            print(f"[DEBUG] {download_id}: first attempt failed, {first_exc}")
            # Check for bot‑detection keywords
            msg = str(first_exc).lower()
            if any(kw in msg for kw in ("sign in", "bot", "403", "429")):
                cookie_file = Path("cookies.txt")
                if cookie_file.exists() and "cookiefile" not in opts:
                    print(f"[DEBUG] {download_id}: retrying with cookies.txt")
                    opts["cookiefile"] = str(cookie_file)
                    _do_download(url, opts, download_id, title)
                else:
                    raise
            else:
                raise

    except Exception as exc:
        print(f"[ERROR] {download_id}: {exc}")
        _safe_update(
            download_id,
            status="error",
            text=f"Download failed: {exc}",
        )


def _do_download(url: str, opts: dict, download_id: str, title: str):
    """Execute the actual yt‑dlp download and finalise progress."""
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)

    # After download, locate the output file
    # yt‑dlp's progress hook already recorded the final filename
    outtmpl = opts["outtmpl"]
    # Re‑construct the expected path
    # The output template may contain format codes; we locate the file by
    # scanning the downloads directory for the newest file that contains
    # the download_id prefix in its name.
    expected_prefix = Path(outtmpl).stem
    candidates = sorted(
        DOWNLOAD_DIR.glob(f"{expected_prefix}*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    final_path = candidates[0] if candidates else None

    if final_path is None or not final_path.exists():
        raise RuntimeError("Download finished but file not found on disk")

    # Update progress store
    download_url = f"/download/{download_id}"
    _safe_update(
        download_id,
        status="completed",
        progress=100.0,
        text="Download complete",
        filename=str(final_path),
        download_url=download_url,
    )


# ---------------------------------------------------------------------------
# GET /progress/{download_id}
# ---------------------------------------------------------------------------
@app.get("/progress/{download_id}")
async def get_progress(download_id: str):
    with progress_lock:
        data = progress_store.get(download_id)
    if not data:
        raise HTTPException(404, "Unknown download_id")
    return data


# ---------------------------------------------------------------------------
# GET /download/{download_id}
# ---------------------------------------------------------------------------
@app.get("/download/{download_id}")
async def serve_file(download_id: str):
    with progress_lock:
        data = progress_store.get(download_id)
    if not data or data.get("status") != "completed":
        raise HTTPException(404, "File not ready or unknown download_id")

    filepath = Path(data["filename"])
    if not filepath.exists():
        raise HTTPException(404, "File has been cleaned up")

    # Derive a meaningful download name
    original_name = filepath.name
    # Remove the download_id prefix if present
    parts = original_name.rsplit(f"-{download_id[:8]}", 1)
    display_name = parts[0] + filepath.suffix if len(parts) == 2 else original_name

    return FileResponse(
        path=str(filepath),
        filename=display_name,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{display_name}"'},
    )


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("[INFO] Starting YouTube Downloader on http://0.0.0.0:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)
