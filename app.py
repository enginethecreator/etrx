import os
import time
import uuid
import threading
import asyncio
import logging
from pathlib import Path
from typing import Dict, Any, List

import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel
import yt_dlp

# --- Configuration & Setup ---
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# Progress storage: {download_id: {"status": ..., "progress": ..., "text": ..., "file_path": ..., "title": ...}}
PROGRESS_STORE: Dict[str, Any] = {}
STORE_LOCK = threading.Lock()

# Cleanup settings
CLEANUP_AFTER_MINUTES = int(os.getenv("CLEANUP_AFTER_MINUTES", 10))

app = FastAPI(title="YouTube Downloader Pro 2026")

# --- Background Cleanup ---
def cleanup_files():
    """Background thread to delete old files and expired progress entries."""
    while True:
        try:
            now = time.time()
            with STORE_LOCK:
                ids_to_remove = []
                for d_id, data in PROGRESS_STORE.items():
                    # If completed/error and old, cleanup
                    timestamp = data.get("timestamp", now)
                    if now - timestamp > (CLEANUP_AFTER_MINUTES * 60):
                        file_path = data.get("file_path")
                        if file_path and os.path.exists(file_path):
                            print(f"[CLEANUP] Deleting expired file: {file_path}")
                            os.remove(file_path)
                        ids_to_remove.append(d_id)
                
                for d_id in ids_to_remove:
                    del PROGRESS_STORE[d_id]
        except Exception as e:
            print(f"[ERROR] Cleanup failed: {e}")
        time.sleep(60)

threading.Thread(target=cleanup_files, daemon=True).start()

# --- yt-dlp Logic ---
def get_yt_dlp_options(download_id: str = None, progress_hook=None):
    """Returns modern yt-dlp options configured for 2026 anti-bot measures."""
    opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'best',
        # Modern Anti-Bot Measures
        'impersonate': '',  # Uses curl_cffi for TLS fingerprinting
        'extractor_args': {
            'youtube': {
                'player_client': ['ios', 'web'],
                'player_skip': ['webpage', 'configs'],
                # Ensure yt-dlp-ejs is utilized if available for JS challenges
                'js_runtime': 'deno', 
            }
        },
        'nocheckcertificate': True,
        'headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Sec-Fetch-Mode': 'navigate',
        }
    }

    if Path("cookies.txt").exists():
        opts['cookiefile'] = "cookies.txt"

    if download_id:
        opts['outtmpl'] = str(DOWNLOAD_DIR / f"%(title)s-{download_id[:8]}.%(ext)s")
    
    if progress_hook:
        opts['progress_hooks'] = [progress_hook]

    return opts

def download_task(download_id: str, url: str, format_id: str):
    def hook(d):
        with STORE_LOCK:
            if d['status'] == 'downloading':
                p = d.get('_percent_str', '0%').replace('%','')
                try:
                    progress_float = float(p)
                except:
                    progress_float = 0.0
                
                PROGRESS_STORE[download_id].update({
                    "status": "downloading",
                    "progress": progress_float,
                    "text": f"Downloading: {d.get('_speed_str', 'N/A')} - {d.get('_eta_str', 'N/A')}"
                })
            elif d['status'] == 'finished':
                PROGRESS_STORE[download_id].update({
                    "status": "processing",
                    "progress": 100,
                    "text": "Finalizing file..."
                })

    try:
        with STORE_LOCK:
            PROGRESS_STORE[download_id]["status"] = "starting"

        opts = get_yt_dlp_options(download_id, hook)
        opts['format'] = format_id

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            
            with STORE_LOCK:
                PROGRESS_STORE[download_id].update({
                    "status": "completed",
                    "progress": 100,
                    "text": "Finished",
                    "file_path": filename,
                    "title": info.get('title', 'video'),
                    "timestamp": time.time()
                })
                print(f"[DEBUG] {download_id}: Download complete. Path: {filename}")

    except Exception as e:
        print(f"[DEBUG] {download_id}: Error occurred: {str(e)}")
        with STORE_LOCK:
            PROGRESS_STORE[download_id].update({
                "status": "error",
                "text": f"Error: {str(e)}",
                "timestamp": time.time()
            })

# --- API Endpoints ---
class URLRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: str

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/formats")
def list_formats(req: URLRequest):
    try:
        opts = get_yt_dlp_options()
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            formats = info.get('formats', [])
            
            results = []
            for f in formats:
                # Filter for combined formats or high-quality matches
                # yt-dlp usually provides 'none' for acodec in video-only streams
                vcodec = f.get('vcodec', 'none')
                acodec = f.get('acodec', 'none')
                
                # We want formats that are playable as single files (contain both or are simple)
                if vcodec != 'none' and acodec != 'none':
                    res = f.get('resolution', f"{f.get('width','?')}x{f.get('height','?')}")
                    size = f.get('filesize') or f.get('filesize_approx')
                    size_mb = f"{round(size / 1024 / 1024, 2)} MB" if size else "Unknown"
                    
                    results.append({
                        "format_id": f.get('format_id'),
                        "resolution": res,
                        "ext": f.get('ext'),
                        "filesize_approx": size_mb,
                        "vcodec": vcodec,
                        "acodec": acodec,
                        "note": f.get('format_note', '')
                    })
            
            # Sort by resolution (naive approach)
            results.sort(key=lambda x: x['resolution'], reverse=True)
            return results
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/download")
def start_download(req: DownloadRequest, background_tasks: BackgroundTasks):
    download_id = str(uuid.uuid4())
    with STORE_LOCK:
        PROGRESS_STORE[download_id] = {
            "status": "starting",
            "progress": 0,
            "text": "Initializing...",
            "timestamp": time.time()
        }
    
    background_tasks.add_task(download_task, download_id, req.url, req.format_id)
    return {"download_id": download_id}

@app.get("/progress/{download_id}")
def get_progress(download_id: str):
    with STORE_LOCK:
        data = PROGRESS_STORE.get(download_id)
        if not data:
            raise HTTPException(status_code=404, detail="Task not found")
        
        resp = {
            "status": data["status"],
            "progress": data["progress"],
            "text": data["text"]
        }
        if data["status"] == "completed":
            resp["download_url"] = f"/download/{download_id}"
        
        return resp

@app.get("/download/{download_id}")
def serve_file(download_id: str):
    with STORE_LOCK:
        data = PROGRESS_STORE.get(download_id)
        if not data or data["status"] != "completed":
            raise HTTPException(status_code=404, detail="File not ready or expired")
        
        file_path = data["file_path"]
        title = data.get("title", "video")
        ext = Path(file_path).suffix
        
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File missing from disk")
    
    return FileResponse(
        path=file_path,
        filename=f"{title}{ext}",
        media_type='application/octet-stream'
    )

@app.get("/", response_class=HTMLResponse)
def index():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>YT Downloader 2026</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .progress-bar { transition: width 0.3s ease; }
    </style>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen flex items-center justify-center p-4">
    <div class="max-w-2xl w-full bg-gray-800 p-8 rounded-2xl shadow-2xl border border-gray-700">
        <h1 class="text-3xl font-bold mb-6 text-center text-blue-400">YouTube Downloader</h1>
        
        <div class="space-y-4">
            <div>
                <label class="block text-sm font-medium mb-1">YouTube URL</label>
                <input type="text" id="url" placeholder="https://www.youtube.com/watch?v=..." 
                    class="w-full bg-gray-700 border border-gray-600 rounded-lg p-3 focus:ring-2 focus:ring-blue-500 outline-none">
            </div>

            <button onclick="getFormats()" id="btn-formats" class="w-full bg-blue-600 hover:bg-blue-500 font-semibold py-3 rounded-lg transition">
                Get Formats
            </button>

            <div id="format-section" class="hidden animate-fade-in">
                <label class="block text-sm font-medium mb-1">Select Quality</label>
                <select id="format-select" class="w-full bg-gray-700 border border-gray-600 rounded-lg p-3 outline-none"></select>
                <button onclick="startDownload()" id="btn-download" class="w-full mt-4 bg-green-600 hover:bg-green-500 font-semibold py-3 rounded-lg transition">
                    Download Video
                </button>
            </div>

            <div id="status-section" class="hidden space-y-2 py-4">
                <div class="flex justify-between text-sm">
                    <span id="status-text">Starting...</span>
                    <span id="percent-text">0%</span>
                </div>
                <div class="w-full bg-gray-700 rounded-full h-3">
                    <div id="progress-bar" class="progress-bar bg-blue-500 h-3 rounded-full" style="width: 0%"></div>
                </div>
                <div id="finish-section" class="hidden text-center pt-2">
                    <a id="download-link" href="#" class="text-blue-400 hover:underline font-bold text-lg">Click here to save file</a>
                </div>
            </div>
        </div>
    </div>

    <script>
        let currentId = null;
        let pollInterval = null;

        async function getFormats() {
            const url = document.getElementById('url').value;
            if(!url) return alert("Enter a URL");
            
            const btn = document.getElementById('btn-formats');
            btn.disabled = true;
            btn.innerText = "Loading...";

            try {
                const res = await fetch('/formats', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ url })
                });
                const data = await res.json();
                if(!res.ok) throw new Error(data.detail || "Failed to fetch formats");

                const select = document.getElementById('format-select');
                select.innerHTML = '';
                data.forEach(f => {
                    const opt = document.createElement('option');
                    opt.value = f.format_id;
                    opt.innerText = `${f.resolution} (${f.ext}) - ${f.filesize_approx} [${f.vcodec}/${f.acodec}] ${f.note}`;
                    select.appendChild(opt);
                });

                document.getElementById('format-section').classList.remove('hidden');
            } catch(e) {
                alert(e.message);
            } finally {
                btn.disabled = false;
                btn.innerText = "Get Formats";
            }
        }

        async function startDownload() {
            const url = document.getElementById('url').value;
            const format_id = document.getElementById('format-select').value;
            
            try {
                const res = await fetch('/download', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ url, format_id })
                });
                const data = await res.json();
                currentId = data.download_id;

                document.getElementById('status-section').classList.remove('hidden');
                document.getElementById('finish-section').classList.add('hidden');
                pollProgress();
            } catch(e) {
                alert("Download failed to start");
            }
        }

        function pollProgress() {
            if(pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(async () => {
                const res = await fetch(`/progress/${currentId}`);
                const data = await res.json();

                document.getElementById('status-text').innerText = data.text;
                document.getElementById('percent-text').innerText = Math.round(data.progress) + '%';
                document.getElementById('progress-bar').style.width = data.progress + '%';

                if(data.status === 'completed') {
                    clearInterval(pollInterval);
                    document.getElementById('finish-section').classList.remove('hidden');
                    document.getElementById('download-link').href = data.download_url;
                    window.location.href = data.download_url; // Auto download
                } else if(data.status === 'error') {
                    clearInterval(pollInterval);
                    alert(data.text);
                }
            }, 1000);
        }
    </script>
</body>
</html>
    """

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
