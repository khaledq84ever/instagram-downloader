from flask import Flask, request, jsonify, send_file, render_template
from flask_cors import CORS
import os, uuid, json, re, glob, threading, time, shutil, subprocess
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
FILE_TTL     = 1800
RATE_LIMIT   = 10

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs        = {}
jobs_lock   = threading.Lock()
_rate_store = defaultdict(list)
_rate_lock  = threading.Lock()

COBALT      = 'https://api.cobalt.tools/'
COBALT_HDR  = {
    'Accept':       'application/json',
    'Content-Type': 'application/json',
    'User-Agent':   'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
}


# ── Job persistence ───────────────────────────────────────────────────────────

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
    r'(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)',
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

def make_filename(title, ext='mp4'):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#]', '', title or 'instagram').strip()
    name = re.sub(r'\s+', ' ', name)
    return (name[:80] or 'instagram') + '.' + ext

def _find_ffmpeg():
    p = shutil.which('ffmpeg')
    if p: return p
    for d in ['/nix/var/nix/profiles/default/bin', '/usr/bin', '/usr/local/bin']:
        fp = os.path.join(d, 'ffmpeg')
        if os.path.isfile(fp): return fp
    nix = glob.glob('/nix/store/*/bin/ffmpeg')
    return nix[0] if nix else None


# ── Cobalt API ────────────────────────────────────────────────────────────────

def cobalt_get_url(ig_url, fmt='mp4'):
    payload = {'url': ig_url}
    if fmt == 'mp3':
        payload['downloadMode'] = 'audio'
    try:
        r = req_lib.post(COBALT, json=payload, headers=COBALT_HDR, timeout=20)
        data = r.json()
        status = data.get('status', '')
        if status in ('tunnel', 'redirect'):
            return data.get('url'), None
        if status == 'picker':
            items = data.get('picker', [])
            if items:
                return items[0].get('url'), None
        return None, data.get('error', {}).get('code', 'Could not get download URL.')
    except Exception as e:
        return None, 'Download service unavailable. Please try again.'

def cobalt_info(ig_url):
    """Get basic info via oEmbed then verify with cobalt."""
    title, thumbnail, uploader = 'Instagram Post', '', ''
    try:
        oe = req_lib.get(
            'https://graph.facebook.com/v18.0/instagram_oembed',
            params={'url': ig_url, 'omitscript': True},
            headers={'User-Agent': COBALT_HDR['User-Agent']},
            timeout=10)
        if oe.status_code == 200:
            d = oe.json()
            title    = d.get('title', '') or title
            uploader = d.get('author_name', '')
            thumbnail = d.get('thumbnail_url', '')
    except Exception:
        pass
    return {'title': title, 'thumbnail': thumbnail, 'uploader': uploader,
            'duration': '—', 'duration_sec': 0}


# ── Job helpers ───────────────────────────────────────────────────────────────

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

def download_stream(src_url, output_path, job_id):
    r = req_lib.get(src_url, stream=True, timeout=120,
                    headers={'User-Agent': COBALT_HDR['User-Agent'],
                             'Referer': 'https://www.instagram.com/'})
    r.raise_for_status()
    total = int(r.headers.get('content-length', 0))
    done  = 0
    with open(output_path, 'wb') as f:
        for chunk in r.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)
                done += len(chunk)
                if total:
                    pct = min(int(done / total * 90), 90)
                    with jobs_lock:
                        if jobs.get(job_id, {}).get('status') == 'processing':
                            jobs[job_id]['progress'] = pct

def do_download(job_id, url, title, fmt):
    _set_job(job_id, {'status': 'processing', 'progress': 5})
    try:
        src_url, err = cobalt_get_url(url, fmt)
        if err or not src_url:
            _set_job(job_id, {'status': 'error', 'error': err or 'No download URL found.'})
            return

        file_id  = str(uuid.uuid4())
        ext      = 'mp3' if fmt == 'mp3' else 'mp4'
        tmp_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.{ext}')

        download_stream(src_url, tmp_path, job_id)
        _set_job(job_id, {'progress': 95})

        # If MP3 requested but file came as mp4, extract audio
        if fmt == 'mp3' and tmp_path.endswith('.mp4'):
            mp3_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp3')
            ffmpeg = _find_ffmpeg()
            if ffmpeg:
                subprocess.run([ffmpeg, '-i', tmp_path, '-q:a', '0', '-map', 'a',
                                mp3_path, '-y'], capture_output=True, timeout=120)
                if os.path.exists(mp3_path):
                    os.remove(tmp_path)
                    tmp_path = mp3_path

        filename = make_filename(title or 'instagram', 'mp3' if fmt == 'mp3' else 'mp4')
        _set_job(job_id, {'status': 'done', 'file': tmp_path,
                           'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, tmp_path)

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
    return jsonify({"name":"InstaGet","short_name":"InstaGet",
                    "description":"Download Instagram videos and photos",
                    "start_url":"/","display":"standalone",
                    "background_color":"#0a0a0a","theme_color":"#833ab4","icons":[]})

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
    info = cobalt_info(url)
    info['url'] = url
    return jsonify(info)

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

    threading.Thread(target=do_download,
                     args=(job_id, url, title or None, fmt), daemon=True).start()
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
