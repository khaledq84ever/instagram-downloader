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

_MOBILE_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) '
              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1')
_HEADERS = {
    'User-Agent':      _MOBILE_UA,
    'Accept':          'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection':      'keep-alive',
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
    r'instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)',
    re.IGNORECASE)

def is_valid_url(url):
    return bool(_IG_RE.search(url))

def extract_shortcode(url):
    m = _IG_RE.search(url)
    return m.group(1) if m else None

def normalize_url(url):
    url = url.strip()
    if not url.startswith('http'):
        url = 'https://' + url
    sc = extract_shortcode(url)
    return f'https://www.instagram.com/p/{sc}/' if sc else url

def make_filename(title, ext='mp4'):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f#@]', '', title or 'instagram').strip()
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


# ── Instagram via yt-dlp (same "trick" as YouTube) + cloudscraper fallback ──

INSTA_COOKIE_FILE = os.environ.get('INSTA_COOKIE_FILE', '')
PROXY_URL = os.environ.get('PROXY_URL', '')
YTDLP_PATH = shutil.which('yt-dlp')


def ig_scrape(shortcode):
    url = f'https://www.instagram.com/p/{shortcode}/'

    # Method 1: yt-dlp with optional cookies
    if YTDLP_PATH:
        try:
            cmd = [YTDLP_PATH, '--dump-json', '--no-warnings', url]
            if INSTA_COOKIE_FILE and os.path.exists(INSTA_COOKIE_FILE):
                cmd += ['--cookies', INSTA_COOKIE_FILE]
            if PROXY_URL:
                cmd += ['--proxy', PROXY_URL]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.returncode == 0 and result.stdout.strip():
                data = json.loads(result.stdout)
                return {
                    'video_url': data.get('url', ''),
                    'thumb_url': data.get('thumbnail', ''),
                    'title': data.get('title', 'Instagram Post'),
                    'uploader': data.get('uploader', '') or data.get('channel', ''),
                    'is_video': data.get('ext', '') in ('mp4', 'mov', 'webm'),
                }, None
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

    # Method 2: SnapInsta via cloudscraper (bypasses Cloudflare)
    try:
        import cloudscraper
        scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})
        r = scraper.get('https://snapinsta.to/en2', timeout=15)
        html = r.text
        k_token = re.search(r'k_token\s*=\s*"([^"]+)"', html)
        k_exp = re.search(r'k_exp\s*=\s*"([^"]+)"', html)
        k_ver = re.search(r'k_ver\s*=\s*"([^"]+)"', html)
        if k_token and k_exp and k_ver:
            resp = scraper.post('https://snapinsta.to/api/ajaxSearch', data={
                'q': url, 't': 'media', 'v': k_ver.group(1),
                'lang': 'en', 'cftoken': k_token.group(1), 'html': '',
            }, headers={'Origin': 'https://snapinsta.to', 'Referer': 'https://snapinsta.to/en2',
                       'X-Requested-With': 'XMLHttpRequest'}, timeout=20)
            data = resp.json()
            if data.get('status') == 'ok' and data.get('data'):
                return {'video_url': data['data'], 'thumb_url': '', 'title': 'Instagram Post',
                        'uploader': '', 'is_video': True}, None
    except ImportError:
        pass
    except Exception as e:
        pass

    return None, 'Could not fetch post. Instagram requires login or cookies.'


# ── Worker ────────────────────────────────────────────────────────────────────

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

def download_stream(src_url, output_path, job_id):
    r = req_lib.get(src_url, stream=True, timeout=120,
                    headers={**_HEADERS, 'Referer': 'https://www.instagram.com/'})
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

def do_download(job_id, shortcode, title, fmt):
    _set_job(job_id, {'status': 'processing', 'progress': 5})
    try:
        data, err = ig_scrape(shortcode)
        if err or not data:
            _set_job(job_id, {'status': 'error', 'error': err or 'Could not fetch post.'}); return

        src_url = data['video_url'] if data['is_video'] else data['thumb_url']
        if not src_url:
            _set_job(job_id, {'status': 'error', 'error': 'No media URL found in this post.'}); return

        file_id  = str(uuid.uuid4())
        tmp_ext  = 'mp4' if data['is_video'] else 'jpg'
        tmp_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.{tmp_ext}')

        download_stream(src_url, tmp_path, job_id)
        _set_job(job_id, {'progress': 92})

        if fmt == 'mp3' and data['is_video']:
            mp3_path = os.path.join(DOWNLOAD_DIR, f'{file_id}.mp3')
            ffmpeg = _find_ffmpeg()
            if ffmpeg:
                subprocess.run([ffmpeg, '-i', tmp_path, '-q:a', '0', '-map', 'a',
                                mp3_path, '-y'], capture_output=True, timeout=120)
                if os.path.exists(mp3_path):
                    os.remove(tmp_path); tmp_path = mp3_path

        t   = title or data.get('title') or f'instagram_{shortcode}'
        ext = 'mp3' if (fmt == 'mp3' and data['is_video']) else tmp_ext
        filename = make_filename(t, ext)
        _set_job(job_id, {'status': 'done', 'file': tmp_path,
                           'filename': filename, 'progress': 100})
        schedule_cleanup(job_id, tmp_path)

    except Exception:
        _set_job(job_id, {'status': 'error', 'error': 'Download failed. Please try again.'})


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

@app.route('/ads.txt')
def ads_txt():
    return 'google.com, pub-3956390078338144, DIRECT, f08c47fec0942fa0\n', 200, {'Content-Type': 'text/plain'}

@app.route('/info', methods=['POST'])
def get_info():
    if not _check_rate(_client_ip()):
        return jsonify({'error': 'Too many requests. Please wait a moment.'}), 429
    data = request.get_json() or {}
    url  = normalize_url(data.get('url', '').strip())
    if not url or not is_valid_url(url):
        return jsonify({'error': 'Invalid Instagram URL — paste a post, Reel, or IGTV link.'}), 400
    sc = extract_shortcode(url)
    if not sc:
        return jsonify({'error': 'Could not parse Instagram URL.'}), 400
    post, err = ig_scrape(sc)
    if err or not post:
        return jsonify({'error': err or 'Could not fetch post.'}), 400
    return jsonify({
        'title':        post['title'],
        'thumbnail':    post['thumb_url'],
        'uploader':     post['uploader'],
        'is_video':     post['is_video'],
        'duration':     '—',
        'duration_sec': 0,
        'url':          url,
    })

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
    sc = extract_shortcode(url)
    if not sc:
        return jsonify({'error': 'Could not parse Instagram URL'}), 400

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {'status': 'pending', 'file': None, 'filename': None,
                         'error': None, 'progress': 0}
        _save_job(job_id, jobs[job_id])

    threading.Thread(target=do_download,
                     args=(job_id, sc, title or None, fmt), daemon=True).start()
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
    mime = 'audio/mpeg' if safe.endswith('.mp3') else ('image/jpeg' if safe.endswith('.jpg') else 'video/mp4')
    return send_file(path, as_attachment=True, download_name=safe, mimetype=mime)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
