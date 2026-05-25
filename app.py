import os
import uuid
import subprocess
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# STARTUP
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent
COOKIES     = BASE_DIR / "cookies.txt"
DOWNLOADS   = BASE_DIR / "downloads"
CLIPS       = BASE_DIR / "clips"

DOWNLOADS.mkdir(exist_ok=True)
CLIPS.mkdir(exist_ok=True)

app = FastAPI(title="ClipForge")

# ---------------------------------------------------------------------------
# IN-MEMORY JOB STORE
# ---------------------------------------------------------------------------
JOBS: dict = {}

def job_set(job_id: str, state: str, data: dict = None, error: str = None):
    JOBS[job_id] = {"state": state, "data": data or {}, "error": error}

# ---------------------------------------------------------------------------
# WORKERS
# ---------------------------------------------------------------------------
def ytdlp_base() -> list:
    """Base yt-dlp command with cookies if available."""
    cmd = ["yt-dlp"]
    if COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    return cmd

def download_worker(job_id: str, url: str):
    try:
        # Get metadata first
        meta_cmd = ytdlp_base() + ["--dump-json", "--no-playlist", url]
        meta_res = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if meta_res.returncode != 0:
            raise RuntimeError(meta_res.stderr.strip()[:400])

        import json
        meta     = json.loads(meta_res.stdout.strip().splitlines()[-1])
        video_id = meta.get("id", "unknown")
        title    = meta.get("title", "untitled")
        duration = meta.get("duration", 0)

        # Sanitize title for filename
        safe_title = "".join(c for c in title if c.isalnum() or c in " -_").strip()[:60]
        out_file   = DOWNLOADS / f"{video_id}_{safe_title}.mp4"

        if out_file.exists():
            job_set(job_id, "done", {
                "video_id": video_id,
                "title": title,
                "duration": duration,
                "filename": out_file.name,
            })
            return

        dl_cmd = ytdlp_base() + [
           "-S", "vcodec:h264,res,acodec:aac",  # Prefer H.264 video, AAC audio
           "--merge-output-format", "mp4",      # Force the container to be MP4
           "--no-playlist",
           "-o", str(out_file),
           url
          ]
        result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-400:])

        job_set(job_id, "done", {
            "video_id": video_id,
            "title": title,
            "duration": duration,
            "filename": out_file.name,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))


def cut_worker(job_id: str, source_filename: str, ts_from: str, ts_to: str):
    try:
        source = DOWNLOADS / source_filename
        if not source.exists():
            raise RuntimeError(f"Source file not found: {source_filename}")

        clip_name = f"clip_{uuid.uuid4().hex[:8]}_{ts_from.replace(':','-')}_{ts_to.replace(':','-')}.mp4"
        out_file  = CLIPS / clip_name

        # Pre-seek before -i = fast keyframe jump, no full decode.
        # -c copy = stream copy, no re-encode. Near-instant for any clip length.
        # The double -ss trick: outer seeks to keyframe, inner trims precisely.
        cmd = [
            "ffmpeg", "-y",
            "-ss", ts_from,
            "-i", str(source),
            "-to", ts_to,
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(out_file)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip()[-400:])

        job_set(job_id, "done", {
            "clip_filename": clip_name,
            "from": ts_from,
            "to": ts_to,
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

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
  --bg:#0f0f11;--surface:#1a1a1f;--surface2:#222228;
  --border:#2e2e38;--accent:#7c5cfc;--accent2:#fc5c7d;
  --text:#e8e8f0;--muted:#6b6b80;--ok:#4caf88;--err:#fc5c5c;
}
body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code','Consolas',monospace;font-size:13px;min-height:100vh}
header{padding:16px 32px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:14px}
header h1{font-size:17px;font-weight:700;letter-spacing:.06em;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.badge{font-size:10px;color:var(--muted);border:1px solid var(--border);padding:2px 8px;border-radius:4px}
.main{max-width:760px;margin:40px auto;padding:0 24px;display:flex;flex-direction:column;gap:28px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;overflow:hidden}
.card-header{padding:16px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px}
.card-header .num{width:24px;height:24px;border-radius:6px;background:var(--accent);color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center}
.card-header .title{font-size:13px;font-weight:600}
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
button.ok-btn{background:#2a6b4a}
button:disabled{opacity:.35;cursor:not-allowed}
.pw{background:var(--border);border-radius:4px;height:3px;overflow:hidden}
.pb{height:100%;width:0%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:4px;transition:width .3s}
.pb.spin{animation:spin 1.4s ease-in-out infinite;width:35%!important}
@keyframes spin{0%{transform:translateX(-100%)}100%{transform:translateX(380%)}}
.msg{font-size:11px;color:var(--muted);min-height:16px;line-height:1.5}
.msg.ok{color:var(--ok)}.msg.err{color:var(--err)}
.info-box{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:14px 16px;display:none}
.info-box .vt{font-weight:600;font-size:13px;margin-bottom:4px}
.info-box .vm{font-size:11px;color:var(--muted)}
.ts-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.dl-row{display:flex;gap:8px;flex-wrap:wrap}
.sep{height:1px;background:var(--border)}
.hint{font-size:11px;color:var(--muted);line-height:1.6}
</style>
</head>
<body>
<header>
  <h1>⬡ CLIPFORGE</h1>
  <span class="badge">yt-dlp · FFmpeg</span>
</header>

<div class="main">

  <!-- STEP 1: DOWNLOAD -->
  <div class="card">
    <div class="card-header">
      <div class="num">1</div>
      <div class="title">Download Video</div>
    </div>
    <div class="card-body">
      <div>
        <label class="lbl">YouTube URL</label>
        <div class="row">
          <input type="text" id="url-input" placeholder="https://youtube.com/watch?v=  or  youtu.be/..."/>
          <button id="dl-btn" onclick="startDownload()">Download</button>
        </div>
      </div>
      <div class="pw"><div class="pb" id="dl-pb"></div></div>
      <div class="msg" id="dl-msg"></div>
      <div class="info-box" id="dl-info">
        <div class="vt" id="dl-title"></div>
        <div class="vm" id="dl-meta"></div>
        <div style="margin-top:12px" class="dl-row">
          <button class="sec" id="dl-full-btn" onclick="downloadFull()" disabled>⬇ Download Full Video</button>
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
      <div class="hint">After downloading, set timestamps and cut a clip from the video.</div>
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
      <div class="row">
        <button id="cut-btn" onclick="startCut()" disabled>✂ Cut Clip</button>
      </div>
      <div class="pw"><div class="pb" id="cut-pb"></div></div>
      <div class="msg" id="cut-msg"></div>
      <div class="info-box" id="cut-info">
        <div class="vt" id="cut-title"></div>
        <div style="margin-top:10px">
          <button class="ok-btn" id="cut-dl-btn" onclick="downloadClip()" disabled>⬇ Download Clip</button>
        </div>
      </div>
    </div>
  </div>

</div>

<script>
let currentFilename = null;
let currentClipFilename = null;

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

function fmtDuration(s) {
  if (!s) return '';
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sec = Math.floor(s%60);
  return h > 0 ? `${h}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`
               : `${m}:${String(sec).padStart(2,'0')}`;
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

  currentFilename     = null;
  currentClipFilename = null;

  document.getElementById('dl-btn').disabled  = true;
  document.getElementById('cut-btn').disabled = true;
  document.getElementById('dl-info').style.display  = 'none';
  document.getElementById('cut-info').style.display = 'none';
  document.getElementById('dl-pb').style.width = '0%';
  setMsg('dl-msg', '', 'Fetching and downloading…');

  try {
    const res  = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    });
    const data = await res.json();

    poll(data.job_id, 'dl-pb', 'dl-msg', (d) => {
      currentFilename = d.filename;
      setMsg('dl-msg', 'ok', '✓ Ready — ' + d.filename);
      document.getElementById('dl-title').textContent = d.title;
      document.getElementById('dl-meta').textContent  = 'Duration: ' + fmtDuration(d.duration) + '  ·  ' + d.filename;
      document.getElementById('dl-info').style.display = '';
      document.getElementById('dl-full-btn').disabled  = false;
      document.getElementById('dl-btn').disabled       = false;
      document.getElementById('cut-btn').disabled      = false;
    }, () => {
      document.getElementById('dl-btn').disabled = false;
    });
  } catch(e) {
    document.getElementById('dl-btn').disabled = false;
    setMsg('dl-msg', 'err', '✗ Request failed');
  }
}

async function startCut() {
  if (!currentFilename) { setMsg('cut-msg', 'err', '✗ Download a video first'); return; }

  const from = document.getElementById('ts-from').value.trim();
  const to   = document.getElementById('ts-to').value.trim();
  if (!from || !to) { setMsg('cut-msg', 'err', '✗ Both timestamps required'); return; }

  currentClipFilename = null;
  document.getElementById('cut-btn').disabled  = true;
  document.getElementById('cut-info').style.display = 'none';
  document.getElementById('cut-pb').style.width = '0%';
  setMsg('cut-msg', '', 'Cutting clip…');

  try {
    const res  = await fetch('/api/cut', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source_filename: currentFilename, ts_from: from, ts_to: to })
    });
    const data = await res.json();

    poll(data.job_id, 'cut-pb', 'cut-msg', (d) => {
      currentClipFilename = d.clip_filename;
      setMsg('cut-msg', 'ok', `✓ Clip ready — ${d.from} → ${d.to}`);
      document.getElementById('cut-title').textContent = d.clip_filename;
      document.getElementById('cut-info').style.display = '';
      document.getElementById('cut-dl-btn').disabled    = false;
      document.getElementById('cut-btn').disabled       = false;
      downloadClip(); // auto-download
    }, () => {
      document.getElementById('cut-btn').disabled = false;
    });
  } catch(e) {
    document.getElementById('cut-btn').disabled = false;
    setMsg('cut-msg', 'err', '✗ Request failed');
  }
}

function downloadFull() {
  if (currentFilename) window.location.href = '/api/download-file/video/' + encodeURIComponent(currentFilename);
}

function downloadClip() {
  if (currentClipFilename) window.location.href = '/api/download-file/clip/' + encodeURIComponent(currentClipFilename);
}

document.getElementById('url-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') startDownload();
});
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

@app.post("/api/cut")
async def api_cut(req: CutRequest):
    job_id = str(uuid.uuid4())
    job_set(job_id, "running")
    threading.Thread(
        target=cut_worker,
        args=(job_id, req.source_filename, req.ts_from, req.ts_to),
        daemon=True
    ).start()
    return {"job_id": job_id}

@app.get("/api/job/{job_id}")
async def api_job(job_id: str):
    return JOBS.get(job_id, {"state": "not_found"})

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
