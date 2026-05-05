import os
import uuid
import threading
import time
from pathlib import Path

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

# ── App Setup ─────────────────────────────────────────

app = FastAPI(title="yt-dlp server", version="5.0.0")

DOWNLOADS_DIR = Path(os.environ.get("DOWNLOADS_DIR", "./downloads"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

COOKIES_FILE = Path("./cookies.txt")

CLEANUP_AFTER_MINUTES = int(os.environ.get("CLEANUP_AFTER_MINUTES", 10))

progress_store = {}

# ── Models ────────────────────────────────────────────

class QuickDownloadRequest(BaseModel):
    url: str


# ── yt-dlp base options (2026 safe config) ────────────

BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "retries": 3,
    "fragment_retries": 3,
    "concurrent_fragment_downloads": 3,

    # Critical for modern YouTube
    "js_runtimes": {"node": {}},
    "remote_components": ["ejs:python"],

    # Stability
    "nocheckcertificate": True,
}


# ── Core download with retry + cookies fallback ───────

def run_yt_dlp(url: str, opts: dict):
    def _run(options):
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl, info

    try:
        return _run(opts)

    except Exception as e:
        err = str(e).lower()

        if any(x in err for x in ["sign in", "bot", "403", "429"]):
            if COOKIES_FILE.exists():
                opts2 = {**opts, "cookiefile": str(COOKIES_FILE)}
                return _run(opts2)

        raise


# ── Download Logic ────────────────────────────────────

def download_video(url: str, download_id: str):
    uid = download_id[:8]
    output_template = str(DOWNLOADS_DIR / f"%(title)s-{uid}.%(ext)s")

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

    opts = {
        **BASE_OPTS,
        "format": "bestvideo[height<=720]+bestaudio/bestvideo[height<=720]+bestaudio[ext=m4a]/best[height<=720]/bestvideo+bestaudio/best",  # keep your stable working selector
        "outtmpl": output_template,
        "progress_hooks": [hook],
    }

    try:
        ydl, info = run_yt_dlp(url, opts)

        filepath = ydl.prepare_filename(info)
        file = Path(filepath)

        if not file.exists():
            raise RuntimeError("file not found after download")

        progress_store[download_id].update({
            "status": "completed",
            "progress": 100,
            "text": "done",
            "filename": file.name,
            "filepath": str(file),
        })

        threading.Thread(
            target=cleanup_file,
            args=(file,),
            daemon=True
        ).start()

    except Exception as e:
        progress_store[download_id].update({
            "status": "error",
            "error": str(e),
        })


# ── Cleanup ───────────────────────────────────────────

def cleanup_file(file: Path):
    time.sleep(CLEANUP_AFTER_MINUTES * 60)
    try:
        if file.exists():
            file.unlink()
    except:
        pass


# ── UI ────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home():
    return """
<!DOCTYPE html>
<html>
<head>
<title>Downloader</title>
<style>
body{font-family:sans-serif;background:#0f172a;color:#fff;text-align:center;padding-top:80px}
input{width:400px;padding:12px;border-radius:8px;border:none}
button{padding:12px 20px;border:none;background:#3b82f6;color:#fff;border-radius:8px;margin-left:10px}
.bar{width:400px;height:8px;background:#333;margin:20px auto;border-radius:4px}
.fill{height:100%;width:0;background:#22c55e}
</style>
</head>
<body>

<h2>yt-dlp downloader</h2>

<input id="url" placeholder="paste url"/>
<button onclick="start()">download</button>

<div class="bar"><div id="fill" class="fill"></div></div>
<p id="text"></p>

<script>
let currentId=null;

async function start(){
    const url=document.getElementById("url").value.trim();
    if(!url)return;

    const res=await fetch("/quick",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({url})
    });

    const data=await res.json();
    currentId=data.download_id;
    poll();
}

async function poll(){
    if(!currentId)return;

    const res=await fetch("/progress/"+currentId);
    const data=await res.json();

    document.getElementById("fill").style.width=data.progress+"%";
    document.getElementById("text").innerText=data.text||data.status;

    if(data.status==="completed"){
        window.location=data.download_url;
        return;
    }

    if(data.status==="error"){
        document.getElementById("text").innerText=data.error;
        return;
    }

    setTimeout(poll,1000);
}
</script>

</body>
</html>
"""


# ── API ───────────────────────────────────────────────

@app.post("/quick")
async def quick_download(req: QuickDownloadRequest):
    download_id = str(uuid.uuid4())

    progress_store[download_id] = {
        "status": "starting",
        "progress": 0,
        "text": "starting...",
    }

    threading.Thread(
        target=download_video,
        args=(req.url, download_id),
        daemon=True
    ).start()

    return {"download_id": download_id}


@app.get("/progress/{download_id}")
async def get_progress(download_id: str):
    data = progress_store.get(download_id)

    if not data:
        return {"status": "not_found"}

    if data.get("status") == "completed":
        data["download_url"] = f"/download/{download_id}"

    return data


@app.get("/download/{download_id}")
async def serve_file(download_id: str):
    data = progress_store.get(download_id)

    if not data or data.get("status") != "completed":
        raise HTTPException(404, "file not ready")

    path = Path(data["filepath"])

    if not path.exists():
        raise HTTPException(404, "file missing")

    return FileResponse(
        path=str(path),
        filename=data["filename"]
    )


@app.get("/health")
def health():
    return {"status": "ok"}
