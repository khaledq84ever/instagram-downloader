from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import subprocess, os, uuid, json, re, glob, threading, time, shutil
import requests as req_lib
from collections import defaultdict

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

app = Flask(__name__)
CORS(app)

DOWNLOAD_DIR = '/tmp/ig_cache'
YTDLP        = os.environ.get('YTDLP_PATH', 'yt-dlp')
FILE_TTL     = 1800
JOB_TIMEOUT  = 300
RATE_LIMIT   = 10

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs      = {}
jobs_lock = threading.Lock()
_rate_store = defaultdict(list)
_rate_lock  = threading.Lock()


def _update_ytdlp():
    try:
        subprocess.run([YTDLP, '--update-to', 'stable'], capture_output=True, timeout=90)
    except Exception:
        pass

threading.Thread(target=_update_ytdlp, daemon=True).start()


# ── Job persistence (shared across threads) ───────────────────────────────────

def _job_path(job_id):
    return os.path.join(DOWNLOAD_DIR, f'job_{job_id}.json')

def _save_job(job_id, job):
    try:
        with open(_job_path(job_id), 'w') as f:
            json.dump(job, f)
    except Exception:
        pass

def _load_job_from_disk(job_id):
    try:
        with open(_job_path(job_id)) as f:
            return json.load(f)
    except Exception:
        return None

def _load_all_jobs():
    for p in glob.glob(os.path.join(DOWNLOAD_DIR, 'job_*.json')):
        try:
            with open(p) as f:
                job = json.load(f)
            job_id = os.path.basename(p)[4:-5]
            if job.get('status') in ('pending', 'processing'):
                job['status'] = 'error'
                job['error']  = 'Server restarted. Please try again.'
                _save_job(job_id, job)
            if job.get('status') == 'done' and not os.path.exists(job.get('file', '')):
                os.remove(p); continue
            jobs[job_id] = job
        except Exception:
            pass

_load_all_jobs()


# ── URL helpers ───────────────────────────────────────────────────────────────

_IG_RE = re.compile(
    r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv|reels)/([A-Za-z0-9_-]+)',
    re.IGNORECASE)

def is_valid_url(url):
    return bool(_IG_RE.search(url))

def normalize_url(url):
    url = url.strip()
    if not url.startswith('http'):
        url = 'https://' + url
    m = _IG_RE.search(url)
    if m:
        return f'https://www.instagram.com/p/{m.group(1)}/'
    return url

def parse_ytdlp_error(stderr):
    err = (stderr or '').lower()
    if 'login' in err or 'checkpoint' in err or 'auth' in err:
        return 'This post requires login. Try a public post or Reel.'
    if 'private' in err:
        return 'This account is private. Only public posts can be downloaded.'
    if 'not found' in err or '404' in err:
        return 'Post not found. Check the link and try again.'
    if 'rate' in err or 'too many' in err:
        return 'Instagram rate limit hit. Please try again in a moment.'
    return 'Could not download this post. Make sure it is public.'

def make_filename(title, ext='mp4'):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#]', '', title or 'instagram').strip()
    name = re.sub(r'\s+', ' ', name)
    return (name[:80] or 'instagram') + '.' + ext

def _find_ffmpeg_dir():
    p = shutil.which('ffmpeg')
    if p: return os.path.dirname(p)
    for d in ['/nix/var/nix/profiles/default/bin', '/usr/bin', '/usr/local/bin']:
        if os.path.isfile(os.path.join(d, 'ffmpeg')): return d
    nix = glob.glob('/nix/store/*/bin/ffmpeg')
    return os.path.dirname(nix[0]) if nix else None

def _set_job(job_id, updates):
    with jobs_lock:
        jobs[job_id].update(updates)
        _save_job(job_id, jobs[job_id])

def schedule_cleanup(job_id, path):
    def _cleanup():
        time.sleep(FILE_TTL)
        try:
            if os.path.isfile(path):  os.remove(path)
            elif os.path.isdir(path): shutil.rmtree(path, ignore_errors=True)
        except Exception: pass
        try: os.remove(_job_path(job_id))
        except Exception: pass
        with jobs_lock: jobs.pop(job_id, None)
    threading.Thread(target=_cleanup, daemon=True).start()


# ── Rate limiter ──────────────────────────────────────────────────────────────

def _check_rate(ip):
    now = time.time()
    with _rate_lock:
        _rate_store[ip] = [t for t in _rate_store[ip] if now - t < 60]
        if len(_rate_store[ip]) >= RATE_LIMIT: return False
        _rate_store[ip].append(now)
        return True

def _client_ip():
    return (request.headers.get('X-Forwarded-For', '').split(',')[0].strip()
            or request.remote_addr or 'unknown')


# ── Worker ────────────────────────────────────────────────────────────────────

_PROGRESS_RE = re.compile(r'\[download\]\s+([\d.]+)%')

def build_cmd(url, output_template, fmt='mp4'):
    headers = [
        '--add-header', 'User-Agent:Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
        '--add-header', 'Accept-Language:en-US,en;q=0.9',
    ]
    if fmt == 'mp3':
        cmd = [YTDLP, '-x', '--audio-format', 'mp3', '--audio-quality', '320K',
               '--no-playlist', '--newline'] + headers
    else:
        cmd = [YTDLP, '-f', 'bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best',
               '--merge-output-format', 'mp4',
               '--no-playlist', '--newline'] + headers
    ffdir = _find_ffmpeg_dir()
    if ffdir: cmd += ['--ffmpeg-location', ffdir]
    cmd += ['-o', output_template, url]
    return cmd

def do_convert(job_id, url, title, fmt):
    _set_job(job_id, {'status': 'processing', 'progress': 0})
    file_id = str(uuid.uuid4())
    output_template = os.path.join(DOWNLOAD_DIR, f'{file_id}.%(ext)s')
    cmd = build_cmd(url, output_template, fmt)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stderr_lines = []

        def _read_stderr():
            for line in proc.stderr:
                stderr_lines.append(line)
                m = _PROGRESS_RE.search(line)
                if m:
                    pct = min(int(float(m.group(1))), 90)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = pct

        t = threading.Thread(target=_read_stderr, daemon=True)
        t.start()
        try:
            proc.wait(timeout=JOB_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            _set_job(job_id, {'status': 'error', 'error': 'Download timed out. Please try again.'})
            return
        t.join(timeout=5)

        if proc.returncode != 0:
            _set_job(job_id, {'status': 'error', 'error': parse_ytdlp_error(''.join(stderr_lines))})
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f'{file_id}.*'))
        if not files:
            _set_job(job_id, {'status': 'error', 'error': 'Output file not found. Please try again.'})
            return

        ext = 'mp3' if fmt == 'mp3' else 'mp4'
        filename = make_filename(title or 'instagram', ext)
        _set_job(job_id, {'status': 'done', 'file': files[0], 'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, files[0])

    except Exception:
        _set_job(job_id, {'status': 'error', 'error': 'Download failed. Please try again.'})


# ── Security headers ──────────────────────────────────────────────────────────

@app.after_request
def add_security_headers(resp):
    resp.headers['X-Frame-Options']        = 'SAMEORIGIN'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy']        = 'strict-origin-when-cross-origin'
    return resp


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/manifest.json')
def manifest():
    return jsonify({
        "name": "InstaGet", "short_name": "InstaGet",
        "description": "Download Instagram videos and photos",
        "start_url": "/", "display": "standalone",
        "background_color": "#0a0a0a", "theme_color": "#833ab4", "icons": []
    })

@app.route('/robots.txt')
def robots():
    return 'User-agent: *\nAllow: /\n', 200, {'Content-Type': 'text/plain'}

@app.route('/info', methods=['POST'])
def get_info():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data = request.get_json() or {}
    url  = normalize_url(data.get('url', '').strip())
    if not url or not is_valid_url(url):
        return jsonify({'error': 'Invalid Instagram URL — paste a post, Reel, or IGTV link.'}), 400
    try:
        result = subprocess.run(
            [YTDLP, '--dump-json', '--no-playlist',
             '--add-header', 'User-Agent:Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15',
             url],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return jsonify({'error': parse_ytdlp_error(result.stderr)}), 400
        info     = json.loads(result.stdout)
        duration = info.get('duration', 0) or 0
        m, s     = divmod(int(duration), 60)
        is_video = info.get('ext') not in ('jpg', 'jpeg', 'png', 'webp')
        return jsonify({
            'title':        info.get('title', '') or info.get('description', '') or 'Instagram Post',
            'thumbnail':    info.get('thumbnail', ''),
            'duration':     f'{m}:{s:02d}' if duration else '—',
            'duration_sec': int(duration),
            'uploader':     info.get('uploader', '') or info.get('channel', ''),
            'is_video':     is_video,
            'url':          url,
        })
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'Request timed out. Please try again.'}), 504
    except Exception:
        return jsonify({'error': 'Failed to fetch post info. Please try again.'}), 500

@app.route('/start', methods=['POST'])
def start_convert():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data  = request.get_json() or {}
    url   = normalize_url(data.get('url', '').strip())
    title = data.get('title', '').strip()
    fmt   = data.get('format', 'mp4')
    if fmt not in ('mp4', 'mp3'): fmt = 'mp4'
    if not is_valid_url(url):
        return jsonify({'error': 'Invalid Instagram URL'}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'pending', 'file': None, 'filename': None,
                         'error': None, 'progress': 0}
        _save_job(job_id, jobs[job_id])

    threading.Thread(target=do_convert, args=(job_id, url, title or None, fmt), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/status/<job_id>')
def get_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        job = _load_job_from_disk(job_id)
        if job:
            with jobs_lock: jobs[job_id] = job
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job.get(k) for k in ('status', 'error', 'filename', 'progress')})

@app.route('/download/<job_id>')
def download_file(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        job = _load_job_from_disk(job_id)
        if job:
            with jobs_lock: jobs[job_id] = job
    if not job or job['status'] != 'done':
        return jsonify({'error': 'File not ready — please try again.'}), 404
    path, filename = job['file'], job['filename']
    if not os.path.exists(path):
        return jsonify({'error': 'File expired. Please download again.'}), 410
    safe = re.sub(r'[^\w\s\-\.\(\)]', '', filename).strip() or 'instagram.mp4'
    mime = 'audio/mpeg' if safe.endswith('.mp3') else 'video/mp4'
    return send_file(path, as_attachment=True, download_name=safe, mimetype=mime)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
