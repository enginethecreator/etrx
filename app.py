import os
import re
import json
import uuid
import threading
import subprocess
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from fastapi import FastAPI, UploadFile, File, Form, Request, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# STARTUP — directories + font resolution
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
for d in ["uploads", "outputs", "temp"]:
    (BASE_DIR / d).mkdir(exist_ok=True)

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

FONT_CAPTION       = find_font(44)
FONT_HOOK          = find_font(56)
FONT_PATH_RESOLVED = next((p for p in KNOWN_FONT_PATHS if os.path.exists(p)), "")

# ---------------------------------------------------------------------------
# INLINE HTML
# ---------------------------------------------------------------------------
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>ClipForge</title>
<style>
  :root{--bg:#0d0d0f;--surface:#16161a;--border:#2a2a32;--accent:#7c5cfc;--accent2:#fc5c7d;--text:#e8e8f0;--muted:#6b6b80;--ok:#4caf88;--err:#fc5c5c}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'SF Mono','Fira Code',monospace;font-size:13px;min-height:100vh}
  header{padding:16px 28px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px}
  header h1{font-size:16px;font-weight:700;letter-spacing:.08em}
  header span{font-size:10px;color:var(--muted);background:var(--surface);border:1px solid var(--border);padding:3px 8px;border-radius:4px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:1px;background:var(--border);height:calc(100vh - 53px)}
  .panel{background:var(--bg);padding:20px;overflow-y:auto;display:flex;flex-direction:column;gap:12px}
  .plabel{font-size:10px;color:var(--muted);letter-spacing:.15em;text-transform:uppercase;margin-bottom:2px}
  .ptitle{font-size:14px;font-weight:600}
  input[type=text],textarea{width:100%;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:9px 11px;border-radius:6px;font-family:inherit;font-size:12px;resize:vertical;outline:none;transition:border-color .15s}
  input[type=text]:focus,textarea:focus{border-color:var(--accent)}
  textarea{min-height:100px}
  button{background:var(--accent);color:#fff;border:none;padding:9px 16px;border-radius:6px;font-family:inherit;font-size:12px;font-weight:600;cursor:pointer;transition:opacity .15s,transform .1s;letter-spacing:.04em}
  button:hover{opacity:.85}
  button:active{transform:scale(.98)}
  button.sec{background:var(--surface);border:1px solid var(--border);color:var(--text)}
  button:disabled{opacity:.4;cursor:not-allowed}
  .row{display:flex;gap:8px;align-items:flex-start}
  .row input{flex:1}
  .pw{background:var(--surface);border-radius:4px;height:4px;overflow:hidden}
  .pb{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));width:0%;transition:width .3s;border-radius:4px}
  .pb.run{animation:ind 1.4s ease-in-out infinite;width:40%!important}
  @keyframes ind{0%{transform:translateX(-100%)}100%{transform:translateX(350%)}}
  .msg{font-size:11px;color:var(--muted);min-height:15px}
  .msg.ok{color:var(--ok)}.msg.err{color:var(--err)}
  .card{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px 14px;display:flex;gap:12px;align-items:center}
  .card img{width:76px;height:43px;object-fit:cover;border-radius:4px;background:var(--border);flex-shrink:0}
  .ctitle{font-weight:600;font-size:13px;margin-bottom:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:240px}
  .cmeta{color:var(--muted);font-size:11px}
  .tabs{display:flex;border-bottom:1px solid var(--border)}
  .tab{padding:7px 13px;cursor:pointer;font-size:11px;color:var(--muted);border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .15s}
  .tab.active{color:var(--accent);border-bottom-color:var(--accent)}
  .tc{display:none;flex-direction:column;gap:8px}
  .tc.active{display:flex}
  .cw{position:relative}
  .cw textarea{padding-right:72px}
  .cbtn{position:absolute;top:8px;right:8px;padding:3px 9px;font-size:10px}
  .tg{display:flex;background:var(--surface);border:1px solid var(--border);border-radius:6px;overflow:hidden}
  .tgb{flex:1;padding:8px;text-align:center;cursor:pointer;font-size:11px;color:var(--muted);transition:all .15s}
  .tgb.active{background:var(--accent);color:#fff;font-weight:600}
  .clip-row{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:9px 13px;display:flex;align-items:center;gap:10px}
  .clip-row .nm{flex:1;font-size:12px;word-break:break-all}
  .clip-row .sz{color:var(--muted);font-size:11px;white-space:nowrap}
  .clip-row a{color:var(--accent);font-size:11px;text-decoration:none;white-space:nowrap}
  .clip-row a:hover{text-decoration:underline}
  .fb{background:var(--surface);border:1px dashed var(--border);border-radius:6px;padding:14px;text-align:center;color:var(--muted);font-size:11px;cursor:pointer;transition:border-color .15s;display:block}
  .fb:hover{border-color:var(--accent);color:var(--text)}
  .fb input{display:none}
  .sep{height:1px;background:var(--border)}
  .lbl{font-size:10px;color:var(--muted);letter-spacing:.12em;text-transform:uppercase;display:block;margin-bottom:5px}
  .hint{font-size:10px;color:var(--muted);line-height:1.6}
  .col{display:flex;flex-direction:column;gap:10px}
</style>
</head>
<body>
<header>
  <h1>&#11041; CLIPFORGE</h1>
  <span>FastAPI &middot; yt-dlp &middot; FFmpeg &middot; OpenCV</span>
</header>
<div class="grid">

  <!-- P01: EXTRACT -->
  <div class="panel">
    <div><div class="plabel">Panel 01</div><div class="ptitle">Extract</div></div>
    <div class="row">
      <input type="text" id="eu" placeholder="YouTube URL — any format"/>
      <button id="eb" onclick="doExtract()">Extract</button>
    </div>
    <div class="pw"><div class="pb" id="ep"></div></div>
    <div class="msg" id="es"></div>
    <div id="mc" style="display:none">
      <div class="card">
        <img id="mt" src="" alt=""/>
        <div><div class="ctitle" id="mti"></div><div class="cmeta" id="ms"></div></div>
      </div>
    </div>
  </div>

  <!-- P02: TRANSCRIPT -->
  <div class="panel">
    <div><div class="plabel">Panel 02</div><div class="ptitle">Transcript</div></div>
    <div class="tabs">
      <div class="tab active" onclick="stab('full')">Full Text</div>
      <div class="tab" onclick="stab('struct')">Structured</div>
      <div class="tab" onclick="stab('raw')">Raw Segments</div>
    </div>
    <div class="tc active" id="tab-full">
      <div class="cw"><textarea id="o-full" rows="13" readonly placeholder="Full transcript..."></textarea>
      <button class="cbtn sec" onclick="cp('o-full',this)">Copy</button></div>
    </div>
    <div class="tc" id="tab-struct">
      <div class="hint">&#8680; Direct input to the strategist prompt &mdash; copy this array</div>
      <div class="cw"><textarea id="o-tr" rows="13" readonly placeholder="Merged sentence-level [{start,end,text}]..."></textarea>
      <button class="cbtn sec" onclick="cp('o-tr',this)">Copy</button></div>
    </div>
    <div class="tc" id="tab-raw">
      <div class="cw"><textarea id="o-seg" rows="13" readonly placeholder="Raw yt-dlp [{start,duration,text}]..."></textarea>
      <button class="cbtn sec" onclick="cp('o-seg',this)">Copy</button></div>
    </div>
  </div>

  <!-- P03: RENDER -->
  <div class="panel">
    <div><div class="plabel">Panel 03</div><div class="ptitle">Render</div></div>
    <div class="tg">
      <div class="tgb active" id="ts" onclick="smode('sliced')">SLICED_FROM_SOURCE</div>
      <div class="tgb" id="ty" onclick="smode('synth')">SYNTHETIC_FROM_SCRATCH</div>
    </div>
    <div id="m-sliced" class="col">
      <div><label class="lbl">Source YouTube URL</label>
        <input type="text" id="ru" placeholder="https://youtube.com/watch?v=..."/></div>
      <div><label class="lbl">Clip Blueprint JSON</label>
        <textarea id="rb" rows="8" placeholder='{"timestamp_start":"00:01:32.000","timestamp_end":"00:01:55.200","hook_text_overlay":"..."}'></textarea></div>
      <button onclick="doSliced()">&#11041; Render Clip</button>
    </div>
    <div id="m-synth" class="col" style="display:none">
      <div><label class="lbl">Blueprint JSON</label>
        <textarea id="sb" rows="5" placeholder='{"hook_text_overlay":"...","asset_assembly_instructions":{"text_to_speech_script":"..."}}'></textarea></div>
      <div>
      <label class="lbl">Scene Images (ordered)</label>

      <div class="dropzone" id="img-dropzone" style="border:2px dashed var(--border); border-radius:6px; padding:20px; text-align:center; cursor:pointer;">
  <input type="file" id="si" accept="image/*" multiple style="display:none;" onchange="handleImageSelect(event)"/>
  <div id="il">Drag & drop images here or click to select</div>
  <div id="img-count" style="margin-top:8px; font-size:11px; color:var(--accent);"></div>
</div>
        </div>
      <div><label class="lbl">Audio File (pre-generated TTS)</label>
        <div class="dropzone" id="audio-dropzone" style="border:2px dashed var(--border); border-radius:6px; padding:20px; text-align:center; cursor:pointer;">
  <input type="file" id="sa" accept="audio/*" style="display:none;" onchange="handleAudioSelect(event)"/>
  <div id="al">Drag & drop audio file here or click to select</div>
  <div id="audio-name" style="margin-top:8px; font-size:11px; color:var(--accent);"></div>
</div>
        </div>
      <button onclick="doSynth()">&#11041; Render Synthetic</button>
    </div>
    <div class="sep"></div>
    <div class="pw"><div class="pb" id="rp"></div></div>
    <div class="msg" id="rs"></div>
  </div>

  <!-- P04: CLIP LIBRARY -->
  <div class="panel">
    <div style="display:flex;align-items:center;justify-content:space-between">
      <div><div class="plabel">Panel 04</div><div class="ptitle">Clip Library</div></div>
      <button class="sec" onclick="loadClips()" style="font-size:11px;padding:5px 11px">Refresh</button>
    </div>
    <div id="cl" class="col"><div class="msg">No clips yet.</div></div>
  </div>

</div>
<script>
function stab(n){
  const ns=['full','struct','raw'];
  document.querySelectorAll('.tab').forEach((t,i)=>t.classList.toggle('active',ns[i]===n));
  ns.forEach(id=>document.getElementById('tab-'+id).classList.toggle('active',id===n));
}
function smode(m){
  document.getElementById('m-sliced').style.display=m==='sliced'?'flex':'none';
  document.getElementById('m-synth').style.display=m==='synth'?'flex':'none';
  document.getElementById('ts').classList.toggle('active',m==='sliced');
  document.getElementById('ty').classList.toggle('active',m==='synth');
}
function cp(id,btn){
  navigator.clipboard.writeText(document.getElementById(id).value).then(()=>{
    const o=btn.textContent;btn.textContent='✓';setTimeout(()=>btn.textContent=o,1400);
  });
}


const imgDrop = document.getElementById('img-dropzone');
imgDrop.addEventListener('click', () => document.getElementById('si').click());
imgDrop.addEventListener('dragover', (e) => { e.preventDefault(); imgDrop.style.borderColor = 'var(--accent)'; });
imgDrop.addEventListener('dragleave', () => { imgDrop.style.borderColor = 'var(--border)'; });
imgDrop.addEventListener('drop', (e) => {
  e.preventDefault();
  imgDrop.style.borderColor = 'var(--border)';
  const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
  if (files.length) {
    const dt = new DataTransfer();
    files.forEach(f => dt.items.add(f));
    document.getElementById('si').files = dt.files;
    handleImageSelect({ target: { files: dt.files } });
  }
});

function handleImageSelect(event) {
  const files = Array.from(event.target.files);
  if (files.length) {
    document.getElementById('il').innerHTML = `${files.length} image(s) selected. Drag more to replace.`;
    document.getElementById('img-count').innerHTML = files.map(f => f.name).join(', ');
  } else {
    document.getElementById('il').innerHTML = 'Drag & drop images here or click to select';
    document.getElementById('img-count').innerHTML = '';
  }
}


const audioDrop = document.getElementById('audio-dropzone');
audioDrop.addEventListener('click', () => document.getElementById('sa').click());
audioDrop.addEventListener('dragover', (e) => { e.preventDefault(); audioDrop.style.borderColor = 'var(--accent)'; });
audioDrop.addEventListener('dragleave', () => { audioDrop.style.borderColor = 'var(--border)'; });
audioDrop.addEventListener('drop', (e) => {
  e.preventDefault();
  audioDrop.style.borderColor = 'var(--border)';
  const file = e.dataTransfer.files[0];
  if (file && file.type.startsWith('audio/')) {
    const dt = new DataTransfer();
    dt.items.add(file);
    document.getElementById('sa').files = dt.files;
    handleAudioSelect({ target: { files: dt.files } });
  }
});

function handleAudioSelect(event) {
  const file = event.target.files[0];
  if (file) {
    document.getElementById('al').innerHTML = `Selected: ${file.name}`;
    document.getElementById('audio-name').innerHTML = file.name;
  } else {
    document.getElementById('al').innerHTML = 'Drag & drop audio file here or click to select';
    document.getElementById('audio-name').innerHTML = '';
  }
}

function fl(inp,lid){
  const l=document.getElementById(lid);
  l.textContent=inp.files.length===1?inp.files[0].name:inp.files.length>1?inp.files.length+' files selected':'Click to upload';
}
function rmsg(cls,txt){const e=document.getElementById('rs');e.className='msg'+(cls?' '+cls:'');e.textContent=txt;}

function poll(jid,pid,mid,onDone,onFail){
  const bar=document.getElementById(pid),msg=document.getElementById(mid);
  bar.classList.add('run');
  const iv=setInterval(async()=>{
    try{
      const d=await(await fetch('/api/job/'+jid)).json();
      if(d.state==='done'){
        clearInterval(iv);bar.classList.remove('run');bar.style.width='100%';
        msg.className='msg ok';onDone(d.data);
      }else if(d.state==='error'){
        clearInterval(iv);bar.classList.remove('run');bar.style.width='0%';
        msg.className='msg err';msg.textContent='\u2717 '+(d.error||'error');if(onFail)onFail();
      }
    }catch(e){clearInterval(iv);msg.className='msg err';msg.textContent='\u2717 Network error';}
  },900);
}

async function doExtract(){
  const url=document.getElementById('eu').value.trim();if(!url)return;
  const btn=document.getElementById('eb');btn.disabled=true;
  const es=document.getElementById('es');es.className='msg';es.textContent='Fetching\u2026';
  document.getElementById('ep').style.width='0%';
  document.getElementById('mc').style.display='none';
  try{
    const d=await(await fetch('/api/extract',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})})).json();
    poll(d.job_id,'ep','es',data=>{
      btn.disabled=false;
      es.textContent='\u2713 '+data.title;
      document.getElementById('mt').src=data.thumbnail||'';
      document.getElementById('mti').textContent=data.title||'';
      document.getElementById('ms').textContent=data.video_id+' \u00b7 '+data.duration;
      document.getElementById('mc').style.display='';
      document.getElementById('o-full').value=data.full_text||'';
      document.getElementById('o-tr').value=JSON.stringify(data.transcript,null,2);
      document.getElementById('o-seg').value=JSON.stringify(data.segments,null,2);
    },()=>btn.disabled=false);
  }catch(e){btn.disabled=false;es.className='msg err';es.textContent='\u2717 Request failed';}
}

async function doSliced(){
  const url=document.getElementById('ru').value.trim();
  const bpRaw=document.getElementById('rb').value.trim();
  if(!url||!bpRaw){rmsg('err','\u2717 URL and blueprint required');return;}
  let bp;try{bp=JSON.parse(bpRaw);}catch(e){rmsg('err','\u2717 Invalid JSON');return;}
  rmsg('','Downloading and slicing\u2026');
  document.getElementById('rp').style.width='0%';
  const d=await(await fetch('/api/render/sliced',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,blueprint:bp})})).json();
  poll(d.job_id,'rp','rs',data=>{rmsg('ok','\u2713 '+data.output);loadClips();});
}

async function doSynth(){
  const bpRaw=document.getElementById('sb').value.trim();
  const imgs=document.getElementById('si').files;
  const audio=document.getElementById('sa').files[0];
  if(!bpRaw){rmsg('err','\u2717 Blueprint required');return;}
  if(!imgs.length){rmsg('err','\u2717 Images required');return;}
  if(!audio){rmsg('err','\u2717 Audio required');return;}
  try{JSON.parse(bpRaw);}catch(e){rmsg('err','\u2717 Invalid JSON');return;}
  const fd=new FormData();
  fd.append('blueprint',bpRaw);
  for(const img of imgs)fd.append('images',img);
  fd.append('audio',audio);
  rmsg('','Uploading and rendering\u2026');
  document.getElementById('rp').style.width='0%';
  const d=await(await fetch('/api/render/synthetic',{method:'POST',body:fd})).json();
  poll(d.job_id,'rp','rs',data=>{rmsg('ok','\u2713 '+data.output);loadClips();});
}

async function loadClips(){
  const clips=await(await fetch('/api/clips')).json();
  const list=document.getElementById('cl');
  if(!clips.length){list.innerHTML='<div class="msg">No clips yet.</div>';return;}
  list.innerHTML=clips.map(c=>`<div class="clip-row"><div class="nm">${c.filename}</div><div class="sz">${c.size_mb} MB</div><a href="/api/download/${c.filename}" download="${c.filename}">\u2193 Download</a></div>`).join('');
}

document.getElementById('eu').addEventListener('keydown',e=>{if(e.key==='Enter')doExtract();});
smode('sliced');
loadClips();
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# APP
# ---------------------------------------------------------------------------
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
    lines   = vtt_text.splitlines()
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
            parts = []
            while i < len(lines) and lines[i].strip() and not time_re.search(lines[i]):
                clean = tag_re.sub("", lines[i]).strip()
                if clean:
                    parts.append(clean)
                i += 1
            if parts:
                segs.append({"start": s_start, "duration": s_end - s_start, "text": " ".join(parts)})
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
    merged  = []
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
        meta_res = subprocess.run(
            ["yt-dlp", "--dump-json", "--no-playlist", url],
            capture_output=True, text=True, timeout=60
        )
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
        subprocess.run(
            ["yt-dlp", "--write-auto-subs", "--sub-langs", "en",
             "--sub-format", "json3", "--skip-download", "--output", sub_base, url],
            capture_output=True, timeout=60
        )
        j3_file = f"{sub_base}.en.json3"
        if os.path.exists(j3_file):
            with open(j3_file, "r", encoding="utf-8") as f:
                segments = parse_json3(json.load(f))
            os.remove(j3_file)

        # VTT fallback
        if not segments:
            subprocess.run(
                ["yt-dlp", "--write-auto-subs", "--sub-langs", "en",
                 "--sub-format", "vtt", "--skip-download", "--output", sub_base, url],
                capture_output=True, timeout=60
            )
            vtt_file = f"{sub_base}.en.vtt"
            if os.path.exists(vtt_file):
                with open(vtt_file, "r", encoding="utf-8") as f:
                    segments = parse_vtt(f.read())
                os.remove(vtt_file)

        if not segments:
            raise RuntimeError("No subtitles found — json3 and VTT both failed")

        job_set(job_id, "done", {
            "video_id": video_id,
            "title": title,
            "duration": duration,
            "thumbnail": thumb,
            "full_text": " ".join(s["text"] for s in segments),
            "segments": segments,
            "transcript": merge_to_sentences(segments),
        })
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))

# ---------------------------------------------------------------------------
# RENDER — SLICED_FROM_SOURCE
# ---------------------------------------------------------------------------
def render_sliced_worker(job_id: str, url: str, blueprint: dict):
    raw_path = str(BASE_DIR / "temp" / f"raw_{job_id}.mp4")
    out_path = str(BASE_DIR / "outputs" / f"{job_id}.mp4")
    try:
        subprocess.run(
            ["yt-dlp", "-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]",
             "--output", raw_path, "--no-playlist", url],
            check=True, capture_output=True, timeout=600
        )
        ts_start     = blueprint["timestamp_start"]
        ts_end       = blueprint["timestamp_end"]
        hook         = blueprint.get("hook_text_overlay", "")[:50]
        hook_escaped = hook.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")

        drawtext = (
            f"drawtext=text='{hook_escaped}'"
            f":fontsize=54:fontcolor=white:borderw=3:bordercolor=black"
            f":x=(w-text_w)/2:y=h*0.12:enable='lt(t\\,5)'"
        )
        if FONT_PATH_RESOLVED:
            drawtext += f":fontfile='{FONT_PATH_RESOLVED}'"

        subprocess.run([
            "ffmpeg", "-y",
            "-i", raw_path,
            "-ss", ts_start, "-to", ts_end,
            "-vf", f"crop=ih*9/16:ih,scale=1080:1920,{drawtext}",
            "-c:v", "libx264", "-profile:v", "main", "-level:v", "4.0",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            out_path
        ], check=True, capture_output=True, timeout=300)

        job_set(job_id, "done", {"output": f"{job_id}.mp4", "type": "sliced"})
    except subprocess.CalledProcessError as ex:
        job_set(job_id, "error", error=(ex.stderr.decode()[-400:] if ex.stderr else str(ex)))
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))
    finally:
        if os.path.exists(raw_path):
            os.remove(raw_path)

# ---------------------------------------------------------------------------
# RENDER — SYNTHETIC_FROM_SCRATCH
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
    res = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path],
        capture_output=True, text=True
    )
    try:
        for stream in json.loads(res.stdout).get("streams", []):
            if stream.get("duration"):
                return float(stream["duration"])
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
        base     = W_total // n_scenes
        rem      = W_total % n_scenes
        scene_wc = [base + (1 if i < rem else 0) for i in range(n_scenes)]

        proc = subprocess.Popen([
            "ffmpeg", "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
            "-r", str(FPS), "-i", "-", "-an",
            "-c:v", "libx264", "-profile:v", "main", "-level:v", "4.0",
            "-movflags", "+faststart", silent_path
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        global_frame = 0
        word_idx     = 0

        for scene_i, img_path in enumerate(image_paths):
            w_scene  = scene_wc[scene_i]
            D_scene  = (w_scene / W_total) * D_master
            N_frames = max(int(D_scene * FPS), 1)

            img_bgr = cv2.imread(img_path)
            if img_bgr is None:
                img_bgr = np.zeros((H, W, 3), dtype=np.uint8)
            img_bgr = cv2.resize(img_bgr, (W, H), interpolation=cv2.INTER_LANCZOS4)

            caption_text = " ".join(words[word_idx: word_idx + w_scene])
            word_idx    += w_scene

            for f in range(N_frames):
                scale     = 1.0 + (0.15 * f / max(N_frames - 1, 1))
                new_w     = int(W * scale)
                new_h     = int(H * scale)
                scaled    = cv2.resize(img_bgr, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                cx, cy    = (new_w - W) // 2, (new_h - H) // 2
                frame_bgr = scaled[cy:cy+H, cx:cx+W].copy()
                frame_pil = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
                draw      = ImageDraw.Draw(frame_pil)

                # Z1: captions — yellow, 8pt stroke
                cap_lines = wrap_text_pixels(caption_text, FONT_CAPTION, W - 80)
                cap_y     = int(H * 0.75)
                for line in cap_lines:
                    tw = draw.textlength(line, font=FONT_CAPTION)
                    tx = (W - tw) / 2
                    for dx in range(-4, 5, 4):
                        for dy in range(-4, 5, 4):
                            if dx or dy:
                                draw.text((tx+dx, cap_y+dy), line, font=FONT_CAPTION, fill=(0,0,0))
                    draw.text((tx, cap_y), line, font=FONT_CAPTION, fill=(255,255,0))
                    cap_y += FONT_CAPTION.size + 8

                # Z2: hook — white, first 5s globally
                if global_frame / FPS < 5.0:
                    hook_lines = wrap_text_pixels(hook, FONT_HOOK, W - 80)
                    hook_y     = int(H * 0.12)
                    for line in hook_lines:
                        tw = draw.textlength(line, font=FONT_HOOK)
                        tx = (W - tw) / 2
                        for dx in range(-4, 5, 4):
                            for dy in range(-4, 5, 4):
                                if dx or dy:
                                    draw.text((tx+dx, hook_y+dy), line, font=FONT_HOOK, fill=(0,0,0))
                        draw.text((tx, hook_y), line, font=FONT_HOOK, fill=(255,255,255))
                        hook_y += FONT_HOOK.size + 10

                proc.stdin.write(cv2.cvtColor(np.array(frame_pil), cv2.COLOR_RGB2BGR).tobytes())
                global_frame += 1

        proc.stdin.close()
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg pipe error: {proc.stderr.read().decode()[-300:]}")

        subprocess.run([
            "ffmpeg", "-y",
            "-i", silent_path, "-i", audio_path,
            "-c:v", "copy", "-c:a", "aac",
            "-shortest", "-movflags", "+faststart", out_path
        ], check=True, capture_output=True, timeout=120)

        job_set(job_id, "done", {"output": f"{job_id}.mp4", "type": "synthetic"})
    except Exception as ex:
        job_set(job_id, "error", error=str(ex))
    finally:
        for p in [silent_path] + image_paths + [audio_path]:
            if os.path.exists(p):
                os.remove(p)

# ---------------------------------------------------------------------------
# ROUTES
# ---------------------------------------------------------------------------
class ExtractRequest(BaseModel):
    url: str

class SlicedRenderRequest(BaseModel):
    url: str
    blueprint: dict

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=HTML)

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
    threading.Thread(target=render_sliced_worker, args=(job_id, req.url, req.blueprint), daemon=True).start()
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

    threading.Thread(
        target=render_synthetic_worker,
        args=(job_id, image_paths, audio_path, json.loads(blueprint)),
        daemon=True
    ).start()
    return {"job_id": job_id}

@app.get("/api/job/{job_id}")
async def api_job_status(job_id: str):
    return JOBS.get(job_id, {"state": "not_found"})

@app.get("/api/clips")
async def api_list_clips():
    return [
        {"filename": f.name, "size_mb": round(f.stat().st_size / 1e6, 2)}
        for f in sorted((BASE_DIR / "outputs").glob("*.mp4"))
    ]

@app.get("/api/download/{filename}")
async def api_download(filename: str):
    path = BASE_DIR / "outputs" / filename
    if not path.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(path), media_type="video/mp4", filename=filename)
