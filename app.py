import os
import uuid
import threading
import time
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string

try:
    import yt_dlp
except ImportError:
    print("yt-dlp not found. Install it with: pip install yt-dlp")
    raise

app = Flask(__name__)

# Configuration
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)
CLEANUP_AFTER_MINUTES = int(os.environ.get('CLEANUP_AFTER_MINUTES', 10))
COOKIES_FILE = Path(os.environ.get("COOKIES_FILE", "./cookies.txt"))

progress_store = {}

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Video Downloader</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        .container {
            background: white;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 40px;
            max-width: 700px;
            width: 100%;
        }
        h1 { color: #333; margin-bottom: 10px; font-size: 28px; }
        .subtitle { color: #666; margin-bottom: 30px; font-size: 14px; }
        .input-group { display: flex; gap: 10px; margin-bottom: 20px; }
        input[type="text"] {
            flex: 1; padding: 15px; border: 2px solid #e0e0e0;
            border-radius: 10px; font-size: 16px; transition: border-color 0.3s;
        }
        input[type="text"]:focus { outline: none; border-color: #667eea; }
        button {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white; border: none; padding: 15px 30px; border-radius: 10px;
            font-size: 16px; cursor: pointer; transition: transform 0.2s, box-shadow 0.2s;
        }
        button:hover { transform: translateY(-2px); box-shadow: 0 5px 20px rgba(102,126,234,0.4); }
        button:disabled { opacity: 0.6; cursor: not-allowed; transform: none; }
        .result { margin-top: 20px; }
        .video-info { background: #f5f5f5; padding: 20px; border-radius: 10px; margin-top: 20px; }
        .video-info h3 { color: #333; margin-bottom: 10px; }
        .progress-bar { width: 100%; height: 8px; background: #e0e0e0; border-radius: 4px; overflow: hidden; margin-top: 10px; }
        .progress-fill { height: 100%; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); width: 0%; transition: width 0.3s; }
        .error { background: #ffebee; color: #c62828; padding: 15px; border-radius: 10px; margin-top: 20px; }
        .download-btn {
            display: inline-block; margin-top: 15px; background: #4caf50; color: white;
            padding: 12px 25px; border-radius: 8px; text-decoration: none; transition: all 0.3s;
        }
        .format-list { max-height: 300px; overflow-y: auto; margin-top: 10px; }
        .format-item {
            display: block; padding: 10px; margin: 5px 0; background: white;
            border: 2px solid #e0e0e0; border-radius: 8px; cursor: pointer;
            transition: border-color 0.3s;
        }
        .format-item:hover { border-color: #667eea; }
        .format-item input[type="radio"] { margin-right: 10px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎬 Video Downloader</h1>
        <p class="subtitle">Download videos from YouTube and 1000+ other platforms using yt-dlp</p>
        
        <div class="input-group">
            <input type="text" id="urlInput" placeholder="Paste video URL here...">
            <button id="infoBtn" onclick="getVideoInfo()">Get Info</button>
        </div>
        
        <div id="result"></div>
    </div>

    <script>
        let currentInfo = null;

        async function getVideoInfo() {
            const url = document.getElementById('urlInput').value;
            if (!url) { showError('Please enter a video URL'); return; }

            const infoBtn = document.getElementById('infoBtn');
            infoBtn.disabled = true;
            infoBtn.textContent = 'Loading...';

            try {
                const response = await fetch('/api/info', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({url: url})
                });
                const data = await response.json();
                if (data.error) { showError(data.error); }
                else {
                    currentInfo = data;
                    displayVideoInfo(data);
                }
            } catch (error) { showError('Failed: ' + error.message); }
            finally { infoBtn.disabled = false; infoBtn.textContent = 'Get Info'; }
        }

        function displayVideoInfo(info) {
            const result = document.getElementById('result');
            
            let html = `
                <div class="video-info">
                    <h3>${info.title || 'Video Information'}</h3>
                    ${info.thumbnail ? `<img src="${info.thumbnail}" style="max-width: 100%; border-radius: 10px; margin: 10px 0;">` : ''}
                    <p><strong>Duration:</strong> ${info.duration || 'N/A'}</p>
                    <p><strong>Uploader:</strong> ${info.uploader || 'N/A'}</p>`;
            
            if (info.formats && info.formats.length > 0) {
                html += `
                    <div style="margin-top: 15px;">
                        <p><em>Default format not available. Please select one below:</em></p>
                        <div class="format-list">`;
                
                info.formats.forEach((f, index) => {
                    const sizeStr = f.filesize ? (f.filesize / 1024 / 1024).toFixed(1) + ' MB' : 'Unknown size';
                    let label = f.resolution + ' - ' + f.ext;
                    if (f.vcodec !== 'none' && f.acodec !== 'none') {
                        label += ' - Combined';
                    } else if (f.vcodec !== 'none') {
                        label += ' - Video Only';
                    } else {
                        label += ' - Audio Only';
                    }
                    label += ' (' + sizeStr + ')';
                    
                    html += `
                        <label class="format-item">
                            <input type="radio" name="formatId" value="${f.format_id}" ${index === 0 ? 'checked' : ''}>
                            <strong>${label}</strong>
                            ${f.format_note ? '<br><small style="color: #666;">' + f.format_note + '</small>' : ''}
                        </label>`;
                });
                
                html += `</div>
                        <button id="downloadBtn" onclick="startDownload(true)" style="width: 100%; margin-top: 15px;">
                            Download Selected Format
                        </button>
                    </div>`;
            } else {
                html += `<button id="downloadBtn" onclick="startDownload(false)" style="width: 100%; margin-top: 20px;">
                            Download Best Quality
                        </button>`;
            }
            
            html += `</div>`;
            result.innerHTML = html;
        }

        function startDownload(useFormatId) {
            if (!currentInfo) return;
            const url = document.getElementById('urlInput').value;
            
            let body = {url: url};
            
            if (useFormatId) {
                const selectedFormat = document.querySelector('input[name="formatId"]:checked');
                if (!selectedFormat) {
                    showError('Please select a format');
                    return;
                }
                body.format_id = selectedFormat.value;
            }
            
            const downloadBtn = document.getElementById('downloadBtn');
            downloadBtn.disabled = true;
            downloadBtn.textContent = 'Downloading...';
            
            const result = document.getElementById('result');
            result.innerHTML += `
                <div style="margin-top: 20px;">
                    <div class="progress-bar"><div class="progress-fill" id="progressFill"></div></div>
                    <p style="text-align: center; margin-top: 10px;" id="progressText">Starting...</p>
                </div>`;

            fetch('/api/download', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) { 
                    // If default format fails, retry with formats list
                    if (data.formats && data.formats.length > 0) {
                        currentInfo = data;
                        displayVideoInfo(data);
                        showError('Default format not available. Select one from the list below.');
                    } else {
                        showError(data.error);
                    }
                    downloadBtn.disabled = false;
                }
                else { checkProgress(data.download_id, downloadBtn); }
            })
            .catch(error => { showError('Failed: ' + error.message); downloadBtn.disabled = false; });
        }

        function checkProgress(downloadId, downloadBtn) {
            fetch(`/api/progress/${downloadId}`)
                .then(response => response.json())
                .then(data => {
                    const progressFill = document.getElementById('progressFill');
                    const progressText = document.getElementById('progressText');
                    if (progressFill) progressFill.style.width = data.progress_percent + '%';
                    if (progressText) progressText.textContent = data.status_text || `Progress: ${data.progress_percent}%`;
                    
                    if (data.status === 'completed') {
                        if (progressText) progressText.textContent = 'Complete!';
                        showDownloadLink(data.filename, data.download_url);
                        downloadBtn.disabled = false;
                        downloadBtn.textContent = downloadBtn.textContent.replace('Downloading...', 'Download');
                    } else if (data.status === 'error') {
                        showError(data.error_message || 'Download failed');
                        downloadBtn.disabled = false;
                    } else setTimeout(() => checkProgress(downloadId, downloadBtn), 1000);
                });
        }

        function showDownloadLink(filename, downloadUrl) {
            const result = document.getElementById('result');
            result.innerHTML += `<div style="margin-top: 20px; text-align: center;">
                <a href="${downloadUrl}" class="download-btn" download>💾 Download ${filename}</a></div>`;
        }

        function showError(message) {
            document.getElementById('result').innerHTML = `<div class="error">❌ ${message}</div>`;
            const downloadBtn = document.getElementById('downloadBtn');
            if (downloadBtn) {
                downloadBtn.disabled = false;
                downloadBtn.textContent = 'Download';
            }
        }
    </script>
</body>
</html>
"""

class VideoDownloader:
    def __init__(self):
        self.ydl_opts = {
            'quiet': True,
            'no_warnings': False,
            'extractor_args': {
                'youtube': {
                    'player_client': ['web'],
                }
            },
            'js_runtimes': {'node': {}},
            'remote_components': ['ejs:python'],
        }
        
        if COOKIES_FILE.exists():
            self.ydl_opts['cookiefile'] = str(COOKIES_FILE)
            print(f"Using cookies from: {COOKIES_FILE}")
        else:
            print(f"No cookies.txt found at {COOKIES_FILE}")
    
    def get_video_info(self, url):
        """Extract video metadata with format list as fallback"""
        with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return self._parse_info(info)
    
    def _parse_info(self, info):
        """Parse info dict and extract formats"""
        formats = []
        seen_ids = set()
        
        for f in info.get('formats', []):
            fid = f.get('format_id', '')
            if fid not in seen_ids and (f.get('vcodec') != 'none' or f.get('acodec') != 'none'):
                seen_ids.add(fid)
                resolution = f.get('resolution') or f.get('format_note') or f'{f.get("height", "?")}p'
                formats.append({
                    'format_id': fid,
                    'ext': f.get('ext', 'unknown'),
                    'resolution': resolution,
                    'filesize': f.get('filesize'),
                    'vcodec': f.get('vcodec', 'none'),
                    'acodec': f.get('acodec', 'none'),
                    'format_note': f.get('format_note', ''),
                })
        
        formats.sort(key=lambda x: (
            0 if (x['vcodec'] != 'none' and x['acodec'] != 'none') 
            else 1 if x['vcodec'] != 'none' 
            else 2
        ))
        
        return {
            'title': info.get('title'),
            'duration': self._format_duration(info.get('duration')),
            'uploader': info.get('uploader'),
            'thumbnail': info.get('thumbnail'),
            'formats': formats,
        }
    
    def download_video(self, url, download_id, format_id=None):
        """Download using default format or specific format_id"""
        output_template = str(DOWNLOAD_DIR / f'%(title)s-{download_id[:8]}.%(ext)s')
        
        opts = {
            **self.ydl_opts,
            'outtmpl': output_template,
            'progress_hooks': [self._progress_hook(download_id)],
            'merge_output_format': 'mp4',
        }
        
        # If format_id provided, use it. Otherwise let yt-dlp pick best.
        if format_id:
            opts['format'] = format_id
        
        progress_store[download_id] = {
            'status': 'starting',
            'progress_percent': 0,
            'status_text': 'Starting...',
            'filename': None,
            'filepath': None,
        }
        
        thread = threading.Thread(target=self._download_task, args=(url, opts, download_id))
        thread.daemon = True
        thread.start()
        return download_id
    
    def _download_task(self, url, opts, download_id):
        """Perform the actual download"""
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                filepath = ydl.prepare_filename(info)
                
                if not os.path.exists(filepath):
                    for f in DOWNLOAD_DIR.iterdir():
                        if download_id[:8] in f.name:
                            filepath = str(f)
                            break
                
                progress_store[download_id].update({
                    'status': 'completed',
                    'progress_percent': 100,
                    'status_text': 'Download complete',
                    'filename': Path(filepath).name,
                    'filepath': filepath,
                })
                
                threading.Thread(target=self._cleanup, args=(filepath,), daemon=True).start()
                
        except Exception as e:
            error_msg = str(e)
            # If format error, extract info to get available formats
            if 'Requested format is not available' in error_msg:
                try:
                    with yt_dlp.YoutubeDL(self.ydl_opts) as ydl:
                        info = ydl.extract_info(url, download=False)
                        formats_info = self._parse_info(info)
                        progress_store[download_id].update({
                            'status': 'error',
                            'error_message': error_msg,
                            'formats': formats_info.get('formats', []),
                        })
                        return
                except:
                    pass
            
            progress_store[download_id].update({
                'status': 'error',
                'error_message': error_msg,
            })
    
    def _progress_hook(self, download_id):
        def hook(d):
            if d['status'] == 'downloading':
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded = d.get('downloaded_bytes', 0)
                percent = (downloaded / total * 100) if total > 0 else 0
                speed = d.get('speed', 0)
                speed_str = f'{speed/1024/1024:.1f} MB/s' if speed else ''
                
                progress_store[download_id].update({
                    'status': 'downloading',
                    'progress_percent': round(percent, 1),
                    'status_text': f'Downloading: {round(percent, 1)}% {speed_str}',
                })
            elif d['status'] == 'finished':
                progress_store[download_id].update({
                    'status': 'processing',
                    'progress_percent': 95,
                    'status_text': 'Processing video...',
                })
        return hook
    
    def _cleanup(self, filepath):
        time.sleep(CLEANUP_AFTER_MINUTES * 60)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
    
    def _format_duration(self, seconds):
        if not seconds: return 'N/A'
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60
        if hours > 0: return f'{hours}:{minutes:02d}:{seconds:02d}'
        return f'{minutes}:{seconds:02d}'

downloader = VideoDownloader()

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/info', methods=['POST'])
def get_video_info():
    data = request.json
    url = data.get('url')
    if not url: return jsonify({'error': 'URL is required'}), 400
    try:
        info = downloader.get_video_info(url)
        return jsonify(info)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
def download_video():
    data = request.json
    url = data.get('url')
    format_id = data.get('format_id')  # Optional
    
    if not url: return jsonify({'error': 'URL is required'}), 400
    
    try:
        download_id = str(uuid.uuid4())
        downloader.download_video(url, download_id, format_id)
        return jsonify({'download_id': download_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/progress/<download_id>')
def get_progress(download_id):
    progress = progress_store.get(download_id, {'status': 'not_found'})
    response = {'download_id': download_id, **progress}
    if progress.get('status') == 'completed':
        response['download_url'] = f'/download/{download_id}'
    return jsonify(response)

@app.route('/download/<download_id>')
def serve_download(download_id):
    progress = progress_store.get(download_id)
    if not progress or progress.get('status') != 'completed':
        return jsonify({'error': 'File not found'}), 404
    filepath = progress.get('filepath')
    if not filepath or not os.path.exists(filepath):
        return jsonify({'error': 'File no longer available'}), 404
    return send_file(filepath, as_attachment=True, download_name=progress.get('filename', 'video.mp4'))

@app.route('/health')
def health():
    return jsonify({'status': 'healthy'})

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
