#!/usr/bin/env python3
"""RaceOne Meeting Recorder — VPS API + PWA host.

端點：
  GET  /                       PWA 前端
  GET  /manifest.json 等        PWA 靜態資產（白名單）
  GET  /health                 健康檢查（公開）
  GET  /transcript/<sid>       逐字稿（?format=json 取結構化）
  GET  /minutes/<sid>          會議紀要（markdown）
  GET  /sessions               已完成的 session 清單
  POST /upload                 錄音切片（multipart，小檔）
  POST /upload-meeting         既有音檔整檔上傳（raw body，串流落盤，適合大檔）
  POST /fetch-link             由分享連結取檔（伺服器端下載，見下）
  POST /end-meeting            合併切片並排入轉錄
  POST /transcribe             手動重跑某 session 的轉錄

POST /fetch-link 的價值：下載發生在**伺服器端**，不經瀏覽器，因此不受
Cloudflare 免費方案 100 MB 上傳上限、也不受前端記憶體限制 —— 400 MB 的
會議錄音也能直接吃。請求立刻回 202，實際下載在背景執行緒進行，進度以
`GET /status/<sid>` 的 `fetch_state` 呈現（downloading / done / failed）。

安全：寫入類端點需 X-Meeting-Token 標頭、?token=/?t= 或 rm_token cookie。
      首次以 /?t=<token> 進站會種下一年期 HttpOnly cookie。
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, os, re, hmac, sys, shutil, threading, time, subprocess, urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import linkfetch

# 路徑一律可用環境變數覆寫（12-factor）。預設值對齊 systemd 佈局，
# 但沒有任何一個預設值會指向特定人的機器。
APP_DIR = Path(os.environ.get('MEETING_APP_DIR',
                              str(Path(__file__).resolve().parent)))
BASE_DIR = Path(os.environ.get('MEETING_DATA_DIR', '/var/lib/meeting-recorder'))
SESS_DIR = BASE_DIR / 'sessions'
UPLOAD_DIR = SESS_DIR / 'uploads'
MERGED_DIR = SESS_DIR / 'merged'
TRANS_DIR = SESS_DIR / 'transcripts'
MINUTES_DIR = SESS_DIR / 'minutes'
QUEUE_DIR = SESS_DIR / 'queue'
META_DIR = SESS_DIR / 'meta'


def write_meta(sid, **kw):
    """寫入 session 的額外資訊（與會者名單、Drive 路徑…）。"""
    META_DIR.mkdir(parents=True, exist_ok=True)
    p = META_DIR / f'{sid}.json'
    try:
        m = json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        m = {}
    m.update(kw)
    p.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding='utf-8')
    return m


def read_meta(sid):
    try:
        return json.loads((META_DIR / f'{sid}.json').read_text(encoding='utf-8'))
    except Exception:
        return {}


def parse_speakers(raw):
    """把「甲,乙,丙」或 list 轉成乾淨的名單。"""
    if isinstance(raw, list):
        items = raw
    else:
        items = str(raw or '').replace('、', ',').replace('，', ',').split(',')
    out = []
    for s in items:
        s = str(s).strip()[:40]
        if s and s not in out:
            out.append(s)
    return out[:20]
WORKER_LOG = BASE_DIR / 'transcribe.log'

PORT = int(os.environ.get('MEETING_PORT', '8765'))
MAX_UPLOAD = 200 * 1024 * 1024        # 200 MB 上限（Cloudflare 免費方案另有 100MB 限制）
# 由分享連結取檔的上限。下載在伺服器端進行，不經瀏覽器／Cloudflare，
# 所以刻意給得比 MAX_UPLOAD 寬（預設 800 MB，可用 FETCH_MAX_MB 調整）。
FETCH_MAX = int(os.environ.get('FETCH_MAX_MB', '800')) * 1024 * 1024

# --- 環境 ---
def _load_env(path: Path):
    if not path.exists():
        return
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        pass


for _p in os.environ.get(
        'MEETING_ENV_FILES',
        '/etc/meeting-recorder/meeting-recorder.env').split(':'):
    if _p.strip():
        _load_env(Path(_p.strip()))
MEETING_TOKEN = os.environ.get('MEETING_TOKEN', '')
TG_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
# 刻意沒有預設值：沒設定就不推播。
# 程式碼裡不該出現任何人的 chat id。
TG_CHAT_ID = os.environ.get('MEETING_CHAT_ID', '')

STATIC_FILES = {
    '/manifest.json': 'application/json; charset=utf-8',
    '/service-worker.js': 'application/javascript; charset=utf-8',
    '/icon-192.png': 'image/png',
    '/icon-512.png': 'image/png',
    '/raceone-logo.jpg': 'image/jpeg',
}
AUDIO_EXT = {'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a', 'audio/m4a': 'm4a',
             'audio/mpeg': 'mp3', 'audio/mp3': 'mp3', 'audio/wav': 'wav',
             'audio/x-wav': 'wav', 'audio/webm': 'webm', 'audio/ogg': 'ogg',
             'audio/aac': 'aac', 'audio/flac': 'flac', 'video/mp4': 'm4a'}


def _san(name, fallback=''):
    """嚴格淨化：只用於 session_id（會變成目錄名），必須 ASCII 安全。"""
    c = re.sub(r'[^A-Za-z0-9_.-]', '_', (name or '').strip())
    return c or fallback


def _safe_label(name, fallback):
    """淨化檔名但保留中日韓字元，讓「語音備忘錄.m4a」這種名字仍可辨識。

    只擋掉路徑分隔、控制字元與其他危險符號。
    """
    s = (name or '').strip().replace('/', '_').replace('\\', '_')
    s = re.sub(r'[\x00-\x1f\x7f]', '', s)
    s = re.sub(r'[<>:"|?*\x00-\x1f]', '_', s)
    s = s.lstrip('.')                       # 避免 .. / 隱藏檔
    return s[:120] or fallback


def notify_telegram(text):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(
            f'https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage',
            data=json.dumps({'chat_id': TG_CHAT_ID, 'text': text}).encode(),
            headers={'Content-Type': 'application/json'}), timeout=15).read()
    except Exception as e:
        print(f'[telegram] {e}', flush=True)


def _pick_worker_python():
    """挑一個能 import faster_whisper 的 python 來跑 worker。

    系統 /usr/bin/python3 是 3.14 且沒有 faster-whisper，
    因此優先用本專案的 venv；萬一 venv 不在，才退回 Hermes venv。
    """
    import shutil
    cands = [
        APP_DIR / 'venv' / 'bin' / 'python',
        Path('/usr/local/lib/hermes-agent/venv/bin/python3'),
    ]
    for c in cands:
        if not Path(c).exists():
            continue
        r = subprocess.run([str(c), '-c', 'import faster_whisper'],
                           capture_output=True, timeout=60)
        if r.returncode == 0:
            return str(c)
    return sys.executable


WORKER_PY = _pick_worker_python()


def _spawn_worker():
    """啟動 worker，並把它關進自己的 cgroup。

    為什麼需要：2026-10-01 實測到，記憶體壓力造成 global OOM 時，核心**殺掉的
    是 Hermes gateway（RSS 最大者），不是 worker**。因此這裡用 systemd-run 建立
    獨立 scope，設定 MemoryMax（超過只殺 worker）並把 OOMScoreAdjust 調高
    （讓核心優先犧牲 worker），確保不會再波及 Hermes 或其他服務。

    MemoryMax 為何是 1800M：主機已於 2026-10 由 1 核/1.9GB 升級為 2 vCPU/4GB，
    轉錄執行緒數同時由 1 提高到 2，實測峰值由 893 MB 升至 1253 MB。
    1800M 給 2 執行緒留了約 550 MB 餘裕；oom_score_adj 仍設高，所以就算真的
    超限，被犧牲的還是 worker，Hermes 不受影響。
    """
    cmd = ['nice', '-n', '19', WORKER_PY, str(APP_DIR / 'transcribe.py'), '--worker']
    if shutil.which('systemd-run'):
        # 注意：scope unit 只支援 cgroup 屬性；Nice / OOMScoreAdjust 是 service-only，
        # 硬塞會被拒（Unknown assignment）並導致 worker 根本沒啟動。
        # 故 Nice 用 nice(1) 包、oom_score_adj 由 transcribe.py 自己寫。
        argv = ['systemd-run', '--scope', '--collect', '--quiet',
                '--unit', f'raceone-transcribe-{int(time.time())}',
                '-p', 'MemoryMax=1800M',
                '-p', 'MemorySwapMax=1500M',
                '--'] + cmd
    else:
        argv = cmd
    WORKER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(WORKER_LOG, 'a') as lf:
        subprocess.Popen(argv, cwd=str(APP_DIR), stdout=lf, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def enqueue_transcription(sid):
    """排入轉錄佇列並喚醒 worker（worker 內以 flock 保證單一實例）。"""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    (QUEUE_DIR / f'{sid}.json').write_text(
        json.dumps({'session_id': sid, 'created': datetime.now().isoformat(),
                    'attempts': 0}))
    _spawn_worker()


def _fetch_link_job(sid, url, name_hint, speakers):
    """背景下載分享連結並排入轉錄。

    為何要背景：400 MB 的會議錄音下載要十幾分鐘，若在請求執行緒裡跑，
    瀏覽器會先逾時。這裡立刻回 202，進度靠 meta 的 `fetch_state` 呈現。
    任何失敗都寫進 meta（不靜默）。
    """
    d = UPLOAD_DIR / sid
    d.mkdir(parents=True, exist_ok=True)
    incoming = d / '_incoming.part'
    try:
        name, size, ctype = linkfetch.fetch_to(url, incoming, FETCH_MAX)
        label = _safe_label(name_hint or name, name or 'meeting.m4a')
        if not Path(label).suffix:
            label += Path(name).suffix or '.m4a'
        # 保險：label 若等於暫存檔名，下一行的 unlink 會把剛下載好的檔案刪掉，
        # 接著 rename 就找不到來源。2026-10-01 實際踩到過（見 linkfetch._drive_name）。
        if label == incoming.name:
            label = 'meeting' + (Path(name).suffix or '.m4a')
        (d / label).unlink(missing_ok=True)
        incoming.replace(d / label)

        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                            'format=duration', '-of', 'default=nw=1:nk=1',
                            str(d / label)],
                           capture_output=True, text=True, timeout=120)
        try:
            dur = float(r.stdout.strip())
        except ValueError:
            dur = 0.0

        print(f'[fetch-link] {sid} {label} {size}B ({size/1048576:.1f} MB) '
              f'dur={dur:.1f}s', flush=True)
        write_meta(sid, filename=label, bytes=size, duration=dur,
                   fetch_state='done', fetch_error=None)
        notify_telegram(f'📥 已由連結取得錄音\nsession: {sid}\n'
                        f'{label} · {size/1048576:.1f} MB · {dur/60:.1f} 分鐘\n'
                        + (f'與會者：{"、".join(speakers)}\n' if speakers else '')
                        + '\n轉錄中，完成後會再通知你。')
        enqueue_transcription(sid)
    except linkfetch.LinkFetchError as e:
        incoming.unlink(missing_ok=True)
        write_meta(sid, fetch_state='failed', fetch_error=str(e))
        print(f'[fetch-link] {sid} 失敗：{e}', flush=True)
        notify_telegram(f'❌ 連結取檔失敗\nsession: {sid}\n{e}')
    except Exception as e:
        incoming.unlink(missing_ok=True)
        write_meta(sid, fetch_state='failed',
                   fetch_error=f'{type(e).__name__}: {e}')
        print(f'[fetch-link] {sid} 未預期錯誤 {type(e).__name__}: {e}', flush=True)


class Handler(BaseHTTPRequestHandler):
    server_version = 'RaceOneMeeting/2.0'
    protocol_version = 'HTTP/1.1'

    # ---------- helpers ----------
    def _path(self):
        return urllib.parse.urlparse(self.path).path

    def _qs(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _cookies(self):
        out = {}
        for part in (self.headers.get('Cookie', '') or '').split(';'):
            if '=' in part:
                k, v = part.split('=', 1)
                out[k.strip()] = v.strip()
        return out

    def _query_token(self):
        q = self._qs()
        return (q.get('token', ['']) or [''])[0] or (q.get('t', ['']) or [''])[0]

    def authorized(self):
        if not MEETING_TOKEN:
            return self.client_address[0] in ('127.0.0.1', '::1')
        supplied = (self.headers.get('X-Meeting-Token', '') or ''
                    or self._query_token()
                    or self._cookies().get('rm_token', ''))
        return bool(supplied) and hmac.compare_digest(supplied, MEETING_TOKEN)

    def _drain(self, limit=16 * 1024 * 1024):
        """把 request body 讀掉。

        為什麼一定要做：HTTP/1.1 keep-alive 下，若我們在還沒讀 body 就回錯誤
        （例如 401），那些殘留位元組會被當成「下一個請求的請求列」解析，
        導致下一個請求變成 501 Unsupported method。
        （2026-10-01 由端到端測試實際抓到。）
        """
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            n = 0
        left = n
        while left > 0:
            buf = self.rfile.read(min(65536, left))
            if not buf:
                break
            left -= len(buf)
        if n > limit:                     # body 太大，連線不該重用
            self.close_connection = True

    # ---------- GET ----------
    def do_GET(self):
        p = self._path()
        # 公開端點：PWA 外殼、靜態檔、健康檢查（給監控用）。
        if p == '/':
            return self.serve_app_shell()
        if p in STATIC_FILES:
            return self.serve_file(APP_DIR / p.lstrip('/'), STATIC_FILES[p])
        if p == '/health':
            return self.send_json({'status': 'ok',
                                   'timestamp': datetime.now().isoformat(),
                                   'queue': len(list(QUEUE_DIR.glob('*.json')))
                                   if QUEUE_DIR.exists() else 0})

        # 以下全部是會議資料（逐字稿／紀要／後設資料…）→ 必須授權。
        # 2026-10-01：這裡原本沒有任何檢查，任何人只要猜到或列舉 session_id
        # （多為毫秒時間戳，且 /sessions 會直接列出）就能讀取所有會議內容。
        if not self.authorized():
            return self.send_json({'error': 'unauthorized'}, 401)

        m = re.fullmatch(r'/transcript/([A-Za-z0-9_.-]+)', p)
        if m:
            return self.serve_transcript(m.group(1))
        m = re.fullmatch(r'/status/([A-Za-z0-9_.-]+)', p)
        if m:
            return self.serve_status(m.group(1))
        m = re.fullmatch(r'/meta/([A-Za-z0-9_.-]+)', p)
        if m:
            return self.send_json(read_meta(m.group(1)))
        if p == '/retention':
            return self.serve_retention()
        m = re.fullmatch(r'/speakers/([A-Za-z0-9_.-]+)', p)
        if m:
            return self.serve_file(TRANS_DIR / f'{m.group(1)}.speakers.txt',
                                   'text/plain; charset=utf-8')
        m = re.fullmatch(r'/minutes/([A-Za-z0-9_.-]+)', p)
        if m:
            return self.serve_file(MINUTES_DIR / f'{m.group(1)}.md',
                                   'text/markdown; charset=utf-8')
        if p == '/sessions':
            return self.send_json({'sessions': self.list_sessions()})
        self.send_json({'error': 'not found'}, 404)

    def serve_app_shell(self):
        src = APP_DIR / 'index.html'
        if not src.exists():
            return self.send_json({'error': 'index.html not found'}, 404)
        html = src.read_text(encoding='utf-8').replace(
            '__AUTHED__', 'true' if self.authorized() else 'false')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')    # 內含驗證狀態
        tok = self._query_token()
        if tok and MEETING_TOKEN and hmac.compare_digest(tok, MEETING_TOKEN):
            self.send_header('Set-Cookie',
                             f'rm_token={MEETING_TOKEN}; Path=/; Max-Age=31536000; '
                             f'Secure; SameSite=Lax; HttpOnly')
        body = html.encode()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def serve_transcript(self, sid):
        if (self._qs().get('format', [''])[0]) == 'json':
            return self.serve_file(TRANS_DIR / f'{sid}.json',
                                   'application/json; charset=utf-8')
        return self.serve_file(TRANS_DIR / f'{sid}.txt', 'text/plain; charset=utf-8')

    def serve_status(self, sid):
        """單一查詢點：轉錄與紀要各自完成了沒。

        為什麼需要：逐字稿會比紀要先出現（紀要還要呼叫 LLM），
        前端若只輪詢逐字稿，會太早去抓紀要而拿到 404。
        """
        t = TRANS_DIR / f'{sid}.txt'
        md = MINUTES_DIR / f'{sid}.md'
        queued = (QUEUE_DIR / f'{sid}.json').exists()
        failed = (QUEUE_DIR / f'{sid}.failed').exists()
        meta = read_meta(sid)
        self.send_json({
            'session_id': sid,
            'queued': queued,
            'failed': failed,
            'transcript': t.exists(),
            'minutes': md.exists(),
            'speakers': meta.get('speakers') or [],
            'has_speakers': (TRANS_DIR / f'{sid}.speakers.txt').exists(),
            'drive_path': meta.get('drive_path'),
            'drive_pending': bool(meta.get('drive_pending')),
            # 由分享連結取檔的進度（None 表示不是走這條路進來的）
            'fetch_state': meta.get('fetch_state'),
            'fetch_error': meta.get('fetch_error'),
            'source_url': meta.get('url'),
            # 歸檔發生在紀要之後（rclone），客戶端要能等到它完成
            'archived': bool(meta.get('drive_path')) and not meta.get('drive_pending'),
            'done': t.exists() and not queued,
        })

    def list_sessions(self):
        out = []
        if not MINUTES_DIR.exists() and not TRANS_DIR.exists():
            return out
        names = {p.stem for p in TRANS_DIR.glob('*.txt')} | {p.stem for p in MINUTES_DIR.glob('*.md')}
        for n in sorted(names):
            t = TRANS_DIR / f'{n}.txt'
            meta = read_meta(n)
            out.append({'session_id': n,
                        'has_transcript': t.exists(),
                        'has_minutes': (MINUTES_DIR / f'{n}.md').exists(),
                        'has_speakers': (TRANS_DIR / f'{n}.speakers.txt').exists(),
                        'speakers': meta.get('speakers') or [],
                        'drive_path': meta.get('drive_path'),
                        'transcript_bytes': t.stat().st_size if t.exists() else 0})
        return out

    def serve_retention(self):
        """回報保留政策與現況（給維運用，不涉機密）。"""
        days = int(os.environ.get('RETENTION_DAYS', '30'))
        cutoff = time.time() - days * 86400
        total = expired = 0
        for root in (UPLOAD_DIR, MERGED_DIR):
            if not root.exists():
                continue
            for p in root.rglob('*'):
                try:
                    if not p.is_file():
                        continue
                    st = p.stat()
                except OSError:
                    continue
                total += st.st_size
                if st.st_mtime < cutoff:
                    expired += 1
        self.send_json({
            'retention_days': days,
            'audio_bytes_now': total,
            'expired_audio_files': expired,
            'kept_forever': ['transcripts', 'minutes', 'meta'],
            'drive_remote': os.environ.get('DRIVE_REMOTE', 'gdrive:Meetings'),
            'drive_audio': os.environ.get('DRIVE_AUDIO', '1'),
        })

    # ---------- POST ----------
    def do_POST(self):
        p = self._path()
        if p not in ('/upload', '/upload-meeting', '/fetch-link',
                     '/end-meeting', '/transcribe'):
            self._drain()
            return self.send_json({'error': 'not found'}, 404)
        if not self.authorized():
            self._drain()
            return self.send_json({'error': 'unauthorized'}, 401)
        if p == '/upload':
            return self.handle_upload()
        if p == '/upload-meeting':
            return self.handle_upload_meeting()
        if p == '/fetch-link':
            return self.handle_fetch_link()
        if p == '/end-meeting':
            return self.handle_end_meeting()
        return self.handle_transcribe()

    # ---------- multipart（切片，小檔）----------
    def parse_multipart(self, body, boundary):
        parts = {}
        for raw in body.split(b'--' + boundary.encode()):
            if not raw or raw.startswith(b'--') or b'\r\n\r\n' not in raw:
                continue
            head, _, content = raw.partition(b'\r\n\r\n')
            if content.endswith(b'\r\n'):
                content = content[:-2]
            nm = re.search(rb'name="([^"]+)"', head)
            if not nm:
                continue
            fn = re.search(rb'filename="([^"]+)"', head)
            ct = re.search(rb'Content-Type:\s*([^\r\n]+)', head)
            parts[nm.group(1).decode()] = {
                'value': content,
                'filename': fn.group(1).decode() if fn else None,
                'content_type': ct.group(1).decode().strip() if ct else None,
            }
        return parts

    def handle_upload(self):
        ct = self.headers.get('Content-Type', '')
        if 'multipart/form-data' not in ct:
            self._drain()
            return self.send_json({'error': 'multipart expected'}, 400)
        bm = re.search(r'boundary=(.+)', ct)
        if not bm:
            self._drain()
            return self.send_json({'error': 'boundary not found'}, 400)
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            self._drain()
            return self.send_json({'error': 'bad content-length'}, 400)
        if length <= 0:
            return self.send_json({'error': 'empty body'}, 400)

        parts = self.parse_multipart(self.rfile.read(length), bm.group(1))

        raw_name = parts.get('audio', {}).get('filename') or \
            f"slice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.webm"
        filename = _san(raw_name, f"slice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.webm")
        rsid = parts.get('session_id', {}).get('value', b'')
        if isinstance(rsid, bytes):
            rsid = rsid.decode('utf-8', 'ignore')
        sid = _san(rsid, 'default')
        data = parts.get('audio', {}).get('value', b'')
        if not data:
            return self.send_json({'error': 'no audio data'}, 400)

        d = UPLOAD_DIR / sid
        d.mkdir(parents=True, exist_ok=True)
        src = d / filename
        src.write_bytes(data)

        mp3 = src.with_suffix('.mp3')
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(src), '-ar', '16000', '-ac', '1',
                        '-c:a', 'libmp3lame', '-b:a', '128k', str(mp3), '-y'],
                       capture_output=True, timeout=120)
        if not mp3.exists():
            return self.send_json({'error': 'ffmpeg conversion failed'}, 500)

        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'default=nw=1:nk=1', str(mp3)],
                           capture_output=True, text=True, timeout=30)
        try:
            dur = float(r.stdout.strip())
        except ValueError:
            dur = 0.0
        n = len(list(d.glob('*.mp3')))
        print(f'[upload] {sid} {filename} {mp3.stat().st_size}B {dur:.1f}s slice#{n}', flush=True)
        self.send_json({'status': 'ok', 'session_id': sid, 'filename': filename,
                        'size': mp3.stat().st_size, 'duration': dur, 'slice_index': n})

    # ---------- 既有音檔整檔上傳（raw body，串流落盤）----------
    def handle_upload_meeting(self):
        q = self._qs()
        sid = _san((q.get('session_id', ['']) or [''])[0], '')
        if not sid or sid == 'default':
            return self.send_json({'error': 'session_id required'}, 400)

        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            self._drain()
            return self.send_json({'error': 'bad content-length'}, 400)
        if length <= 0:
            return self.send_json({'error': 'empty body'}, 400)
        if length > MAX_UPLOAD:
            # 太大，不讀 body；連線必須關閉，否則殘留位元組會污染下一個請求
            self.close_connection = True
            return self.send_json(
                {'error': f'檔案過大 ({length/1048576:.0f} MB)，上限 '
                          f'{MAX_UPLOAD//1048576} MB'}, 413)

        ctype = (self.headers.get('Content-Type', '') or '').split(';')[0].strip().lower()
        ext = AUDIO_EXT.get(ctype)
        if not ext:
            name = (q.get('name', ['']) or [''])[0]
            ext = (Path(name).suffix.lstrip('.') or 'm4a') if name else 'm4a'
            ext = _san(ext, 'm4a')
        raw_name = (q.get('name', ['']) or [''])[0] or f'meeting.{ext}'
        label = _safe_label(raw_name, f'meeting.{ext}')
        if not Path(label).suffix:          # 沒副檔名就補上
            label = f'{label}.{ext}'

        d = UPLOAD_DIR / sid
        d.mkdir(parents=True, exist_ok=True)
        dst = d / label

        # 串流寫入，避免大檔整包進記憶體
        remaining, CHUNK = length, 1024 * 256
        with open(dst, 'wb') as f:
            while remaining > 0:
                buf = self.rfile.read(min(CHUNK, remaining))
                if not buf:
                    break
                f.write(buf)
                remaining -= len(buf)

        written = dst.stat().st_size
        if written != length:
            self.close_connection = True      # 串流不完整，連線不可重用
            return self.send_json(
                {'error': f'incomplete upload ({written}/{length} bytes)'}, 400)

        r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'default=nw=1:nk=1', str(dst)],
                           capture_output=True, text=True, timeout=60)
        try:
            dur = float(r.stdout.strip())
        except ValueError:
            dur = 0.0

        print(f'[upload-meeting] {sid} {label} {written}B ({written/1048576:.1f} MB) '
              f'dur={dur:.1f}s', flush=True)

        # 順序很重要：meta（含與會者名單）必須先寫好，再排入轉錄。
        # 否則 worker 可能在名單寫入前就讀到空的 meta。
        write_meta(sid, filename=label, bytes=written, duration=dur,
                   uploaded_at=datetime.now().isoformat())
        sp = parse_speakers((q.get('speakers', ['']) or [''])[0])
        if sp:
            write_meta(sid, speakers=sp)

        notify_telegram(f'📥 已收到整檔錄音\nsession: {sid}\n'
                        f'{label} · {written/1048576:.1f} MB · {dur/60:.1f} 分鐘\n'
                        + (f'與會者：{"、".join(sp)}\n' if sp else '')
                        + '\n轉錄中，完成後會再通知你。')

        enqueue_transcription(sid)
        self.send_json({'status': 'accepted', 'session_id': sid, 'filename': label,
                        'bytes': written, 'duration': dur,
                        'speakers': sp, 'queued': True}, 202)

    # ---------- 由分享連結取檔 ----------
    def handle_fetch_link(self):
        """接受 {url, name?, speakers?, session_id?}，背景下載後排入轉錄。

        URL 先驗證再回 202 —— 不允許「先接受再失敗」，否則使用者要等
        背景跑完才知道連結打錯。
        """
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            self._drain()
            return self.send_json({'error': 'bad content-length'}, 400)
        try:
            payload = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            return self.send_json({'error': 'invalid json'}, 400)

        try:
            url = linkfetch.validate(payload.get('url') or '')
        except linkfetch.LinkFetchError as e:
            return self.send_json({'error': str(e)}, 400)

        sid = _san(payload.get('session_id', ''), '')
        if not sid or sid == 'default':
            sid = f'link_{int(time.time() * 1000)}'
        sp = parse_speakers(payload.get('speakers') or [])

        write_meta(sid, url=url, fetch_state='downloading',
                   uploaded_at=datetime.now().isoformat())
        if sp:
            write_meta(sid, speakers=sp)

        print(f'[fetch-link] {sid} 開始下載 {url}', flush=True)
        threading.Thread(target=_fetch_link_job,
                         args=(sid, url, (payload.get('name') or '').strip(), sp),
                         daemon=True).start()
        self.send_json({'status': 'fetching', 'session_id': sid,
                        'url': url, 'queued': True}, 202)

    # ---------- 結束會議 ----------
    def handle_end_meeting(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
        except ValueError:
            return self.send_json({'error': 'bad content-length'}, 400)
        try:
            payload = json.loads(self.rfile.read(length) or b'{}')
        except json.JSONDecodeError:
            return self.send_json({'error': 'invalid json'}, 400)

        sid = _san(payload.get('session_id', ''), '')
        if not sid or sid == 'default':
            return self.send_json({'error': 'session_id required'}, 400)

        d = UPLOAD_DIR / sid
        if not d.is_dir():
            return self.send_json({'error': 'no slices found'}, 404)
        # 原始切片（webm）優先；若上傳時已轉檔則退回 mp3。
        # 兩者取同一份清單，避免「檢查用 A、合併用 B」的不一致。
        slices = sorted(d.glob('*.webm'), key=lambda p: p.name) or \
            sorted(d.glob('*.mp3'), key=lambda p: p.name)
        if not slices:
            return self.send_json({'error': 'no slices found'}, 404)

        merged = MERGED_DIR / f'{sid}.mp3'
        merged.parent.mkdir(parents=True, exist_ok=True)
        # 用 ffmpeg filter_complex 合併（concat demuxer 啃不動 Opus/webm 封包，
        # 會產出 0 byte 的 mp3）
        inputs = []
        for s in slices:
            inputs.extend(['-i', str(s)])
        filter_complex = (''.join(f'[{i}:a]' for i in range(len(slices))) +
                          f'concat=n={len(slices)}:v=0:a=1[out]')
        proc = subprocess.run(['ffmpeg', '-v', 'error', *inputs,
                               '-filter_complex', filter_complex,
                               '-map', '[out]', '-c:a', 'libmp3lame', '-b:a', '128k',
                               str(merged), '-y'],
                              capture_output=True, text=True, timeout=600)
        if not merged.exists() or merged.stat().st_size == 0:
            return self.send_json({'error': 'merge failed',
                                   'detail': proc.stderr[-400:]}, 500)

        print(f'[end-meeting] {sid} slices={len(slices)} '
              f'merged={merged.stat().st_size}B', flush=True)
        # 同樣先寫 meta 再排入轉錄（避免 worker 讀不到名單）
        sp = parse_speakers(payload.get('speakers') or [])
        if sp:
            write_meta(sid, speakers=sp)
        write_meta(sid, slice_count=len(slices),
                   merged_bytes=merged.stat().st_size,
                   ended_at=datetime.now().isoformat())
        enqueue_transcription(sid)
        self.send_json({'status': 'ok', 'session_id': sid, 'merged': str(merged),
                        'slices': len(slices), 'size': merged.stat().st_size,
                        'speakers': sp, 'queued': True})

    def handle_transcribe(self):
        try:
            length = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(length) or b'{}')
        except (ValueError, json.JSONDecodeError):
            return self.send_json({'error': 'invalid json'}, 400)
        sid = _san(payload.get('session_id', ''), '')
        if not sid:
            return self.send_json({'error': 'session_id required'}, 400)
        enqueue_transcription(sid)
        self.send_json({'status': 'queued', 'session_id': sid}, 202)

    # ---------- responses ----------
    def serve_file(self, path: Path, ctype: str):
        if not path.exists():
            return self.send_json({'error': f'{path.name} not found'}, 404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} {args[0]}",
              flush=True)


if __name__ == '__main__':
    for d in (UPLOAD_DIR, MERGED_DIR, TRANS_DIR, MINUTES_DIR, QUEUE_DIR):
        d.mkdir(parents=True, exist_ok=True)
    print(f'RaceOne Meeting Recorder :{PORT} '
          f'(auth={"on" if MEETING_TOKEN else "OFF-local-only"})', flush=True)
    print(f'worker python: {WORKER_PY}', flush=True)
    # 啟動自檢：分享連結取檔依賴 gdown。跑錯 python（例如系統 python 而非
    # 專案 venv）時這裡會直接講，不必等到使用者貼連結才發現。
    try:
        import gdown as _g
        print(f'link fetch: on (gdown {_g.__version__}, '
              f'{len(linkfetch.ALLOW_HOSTS)} 個允許主機)', flush=True)
    except Exception as e:
        print(f'link fetch: OFF —— 無法 import gdown（{e}）。'
              f'分享連結取檔會失敗；請確認服務用專案 venv 的 python 執行。',
              flush=True)
    HTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
