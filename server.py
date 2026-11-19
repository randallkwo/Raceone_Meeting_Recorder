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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import json, os, re, hmac, sys, shutil, threading, time, subprocess, urllib.parse, hashlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import linkfetch

APP_DIR = Path('/root/meeting-recorder-app')
BASE_DIR = Path('/root/meeting-recorder')
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


# ---------- 結束會議時容忍「最後一片還在路上」 ----------
# 為什麼需要（2026-10-21 實際發生）：
# 瀏覽器 MediaRecorder 的 onstop 是**非同步**的，最後一片（不滿一個切片週期的
# 尾段）只有在 onstop 之後才會被排進上傳佇列。舊版前端呼叫 stop() 後**不等待**
# onstop，就直接檢查 pending 並送出 /end-meeting → 伺服器當下什麼切片都沒看到，
# 回 404 "no slices found"，而那片音檔 12 秒後才上傳成功。
# 症狀有兩種，第二種更陰險：
#   A. 短錄音（不滿一個切片）：完全沒有切片 → 整場沒有逐字稿
#   B. 長錄音：前面的切片已上傳，只有**尾段被靜默丟掉**（合併時看不到它）
# 這裡用「等待切片出現」把 A 從失敗變成成功；B 由前端的 await stop() 修掉。
END_MEETING_SLICE_WAIT_SEC = float(os.environ.get('END_MEETING_SLICE_WAIT_SEC', '25'))


def _probe_audio(path: Path):
    """驗證音訊檔是否可讀。回傳 (ok: bool, duration: float|None, err: str)。

    2026-11-17 重要的教訓（regression）：
      MediaRecorder 產出的是**串流 WebM**，合法檔案的 format=duration 就是 "N/A"
      —— 串流容器本來就沒有 duration metadata。舊版用 float(duration) 判斷，
      float("N/A") 拋 ValueError，於是**每一片合法切片都被當成壞檔拒收**
      （實測 2026-11-17 整場 34 分鐘會議全滅，log 全是 duration=N/A）。

    正確判準：
      - ffprobe 能讀出音訊串流（codec_type=audio）且 exit code == 0 → 合法
      - 截斷/損毀檔（EBML header parsing failed）→ ffprobe 非零退出 → 拒收
      - duration 只是附加資訊，允許 None（N/A）
    """
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=codec_type',
             '-of', 'default=nw=1:nk=1', str(path)],
            capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False, None, 'ffprobe timeout'
    if r.returncode != 0:
        msg = (r.stderr or '').strip().splitlines()
        return False, None, f'ffprobe error: {msg[0] if msg else "unknown"}'
    if r.stdout.strip() != 'audio':
        return False, None, f'no audio stream (got {r.stdout.strip()!r})'

    # 附加：嘗試取得 duration，允許 N/A
    dur = None
    try:
        r2 = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', str(path)],
            capture_output=True, text=True, timeout=60)
        if r2.returncode == 0:
            dur = float(r2.stdout.strip())
    except (ValueError, subprocess.TimeoutExpired):
        dur = None
    return True, dur, ''


def _repair_missing_header(path: Path, d: Path) -> bool:
    """把缺 EBML header 的切片補回 header（就地覆寫）。回傳是否成功修改。

    背景（2026-11-17）：MediaRecorder 只把 EBML header 放在第一塊，之後的
    ondataavailable 都只是 Cluster。舊版前端把它們切成獨立檔案 → 第 2 片之後
    「EBML header parsing failed」→ 無法轉檔 → 靜默遺失整段會議。

    做法：從同目錄找另一支「能通過 ffprobe 的」webm（通常是第 0 片），
    取其 EBML header（從檔首到第一個 Cluster tag 0x1F43B675 之前），
    前置到 path 開頭。
    """
    if path.suffix.lower() != '.webm':
        return False
    if path.read_bytes()[:4] == b'\x1a\x45\xdf\xa3':   # 已有 EBML magic
        return False
    for donor in sorted(d.glob('*.webm')):
        if donor == path:
            continue
        try:
            head = donor.read_bytes()
        except OSError:
            continue
        if head[:4] != b'\x1a\x45\xdf\xa3':
            continue
        idx = head.find(b'\x1f\x43\xb6\x75')           # 第一個 Cluster tag
        if idx <= 0:
            continue
        try:
            body = path.read_bytes()
            path.write_bytes(head[:idx] + body)
            return True
        except OSError:
            return False
    return False


def _list_slices(d: Path):
    """優先用 mp3（經過 ffmpeg 驗證轉檔成功的檔案）；找不到才退回 webm。

    為什麼優先 mp3：上傳流程是「存 webm → 轉 mp3」，mp3 存在代表 ffmpeg 已驗證該切片可讀。
    webm 可能是瀏覽器最後一片非同步上傳時被截斷的壞檔（2026-11-16 實際踩到：EBML header parsing failed）。
    檢查與合併共用同一份清單，避免「檢查用 A、合併用 B」的不一致。"""
    if not d.is_dir():
        return []
    return sorted(d.glob('*.mp3'), key=lambda p: p.name) or \
        sorted(d.glob('*.webm'), key=lambda p: p.name)


def _wait_for_slices(d: Path, timeout: float):
    """等到切片出現，或超過 timeout。每 0.5 秒重掃（目錄可能在等待期間才被建立）。"""
    deadline = time.monotonic() + timeout
    waited = 0.0
    while True:
        s = _list_slices(d)
        if s or time.monotonic() >= deadline:
            return s, waited
        time.sleep(0.5)
        waited += 0.5


# ---------- 上傳去重（防止誤按多次「上傳並轉錄」） ----------
# 以檔案內容 SHA-256 為鍵。同一份音檔在 DEDUP_WINDOW 秒內重複上傳，
# 直接回傳既有 session，不再排入轉錄、不再佔用 GPU。
UPLOAD_INDEX = SESS_DIR / '.upload_index.json'
DEDUP_WINDOW = 6 * 3600          # 6 小時內視為重複
_index_lock = threading.Lock()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for blk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(blk)
    return h.hexdigest()


def _index_load() -> dict:
    try:
        return json.loads(UPLOAD_INDEX.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _index_lookup(sha: str):
    """回傳 (sid, ts) 或 None。過期項目順手清掉。"""
    now = time.time()
    idx = _index_load()
    hit = idx.get(sha)
    if not hit:
        return None
    sid, ts = hit.get('sid'), hit.get('ts', 0)
    if now - ts > DEDUP_WINDOW:
        idx.pop(sha, None)
        try:
            UPLOAD_INDEX.write_text(json.dumps(idx, ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass
        return None
    # 前一次其實失敗了（或已死掉）→ 不當成重複，讓使用者能重傳。
    # 去重的目的是擋「誤按」，不是擋「重試」。
    if (QUEUE_DIR / f'{sid}.failed').exists():
        return None
    if not (TRANS_DIR / f'{sid}.txt').exists() and \
       not (QUEUE_DIR / f'{sid}.json').exists() and \
       not (UPLOAD_DIR / sid).exists():
        return None                      # 舊 session 已不存在，無從沿用
    return sid, ts


def _index_store(sha: str, sid: str):
    with _index_lock:
        idx = _index_load()
        idx[sha] = {'sid': sid, 'ts': time.time()}
        # 只保留最近 500 筆，避免檔案無限成長
        if len(idx) > 500:
            for k in sorted(idx, key=lambda k: idx[k].get('ts', 0))[:len(idx) - 500]:
                idx.pop(k, None)
        try:
            UPLOAD_INDEX.write_text(json.dumps(idx, ensure_ascii=False), encoding='utf-8')
        except OSError:
            pass


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


_load_env(Path('/root/config/meeting-recorder.env'))
_load_env(Path('/root/.hermes/profiles/raceone-ai/.env'))
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
    # Android 會把圖示裁成圓形／水滴形，需要留白更多的 maskable 版本
    '/icon-maskable-512.png': 'image/png',
    '/apple-touch-icon.png': 'image/png',
    '/raceone-logo.jpg': 'image/jpeg',
    # 官方透明 mark（PWA 圖示就是用它後製疊出來的）
    '/raceone-mark.png': 'image/png',
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
    # 閒置讀取逾時。沒有這個，一個「連上卻不送完 body」的客戶端會讓
    # 伺服器永久卡在 recvfrom()，後續所有請求全部逾時（2026-10-02 實際發生）。
    # 300s 是「兩次讀取之間」的閒置上限，不是總時長 —— 慢速大檔上傳不受影響，
    # 只要持續有資料進來就不會觸發。
    timeout = 300

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
            sp_path = TRANS_DIR / f'{m.group(1)}.speakers.txt'
            return self._nav_md(f'發言歸屬 {m.group(1)}', sp_path,
                                   'text/plain; charset=utf-8')
        # 2026-11-17：紀要 HTML 版（自帶樣式的單檔，供閱讀）。
        # ⚠️ 必須排在下面那條之前：`[A-Za-z0-9_.-]+` 也吃 `.`，
        # 所以 /minutes/abc.html 會被那條搶走並去找 `abc.html.md`。
        m = re.fullmatch(r'/minutes/([A-Za-z0-9_-]+)\.html', p)
        if m:
            return self.serve_minutes_html(m.group(1))
        m = re.fullmatch(r'/minutes/([A-Za-z0-9_.-]+)', p)
        if m:
            md_path = MINUTES_DIR / f'{m.group(1)}.md'
            return self._nav_md(f'會議紀要 {m.group(1)}', md_path,
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

    def _nav_html(self, title, body_html):
        """帶返回導航的 HTML 頁面（修復：點進 transcript/minutes/speakers 後能回主頁）。"""
        html = f'''<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{title}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'SF Pro','PingFang TC','Microsoft JhengHei',sans-serif;
  background:#F9F9F6;color:#1F2937;display:flex;flex-direction:column;padding-bottom:env(safe-area-inset-bottom)}}
header{{background:#FFF;border-bottom:1px solid #E5E7EB;padding:14px 18px;
  display:flex;align-items:center;gap:10px;padding-top:calc(14px + env(safe-area-inset-top))}}
header a{{text-decoration:none;color:#0066FF;font-size:14px;font-weight:600}}
header h1{{font-size:16px}}
pre{{white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;
  font-family:ui-monospace,Menlo,monospace;font-size:13px;
  line-height:1.6;max-height:70vh;overflow-y:auto;background:#FFF;border:1px solid #E5E7EB;
  border-radius:12px;padding:14px;margin:18px}}
</style></head><body>
<header><a href="/">← 返回主頁</a><h1>{title}</h1></header>
{body_html}
</body></html>'''
        return html.encode('utf-8')

    def serve_transcript(self, sid):
        if (self._qs().get('format', [''])[0]) == 'json':
            return self.serve_file(TRANS_DIR / f'{sid}.json',
                                   'application/json; charset=utf-8')
        # 修復：返回帶「返回主頁」導航的 HTML，而非純文本
        txt_path = TRANS_DIR / f'{sid}.txt'
        if txt_path.exists():
            body = txt_path.read_text(encoding='utf-8', errors='replace')
            escaped = (body.replace('&','&amp;').replace('<','&lt;')
                          .replace('>','&gt;'))
            html = (f'<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8">'
                    f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                    f'<title>逐字稿 {sid}</title><style>'
                    f'*{{margin:0;padding:0;box-sizing:border-box}}'
                    f'body{{font-family:-apple-system,sans-serif;background:#F9F9F6;color:#1F2937}}'
                    f'header{{background:#FFF;border-bottom:1px solid #E5E7EB;padding:14px 18px;display:flex;align-items:center;gap:10px}}'
                    f'header a{{text-decoration:none;color:#0066FF;font-size:14px;font-weight:600}}'
                    f'pre{{white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.6;max-height:70vh;overflow-y:auto;background:#FFF;border:1px solid #E5E7EB;border-radius:12px;padding:14px;margin:18px}}'
                    f'</style></head><body><header><a href="/">← 返回主頁</a><h1>逐字稿 {sid}</h1></header>'
                    f'<pre>{escaped}</pre></body></html>').encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(html)))
            self.end_headers()
            self.wfile.write(html)
            return
        return self.send_json({'error': 'transcript not found'}, 404)

    def _nav_md(self, title, file_path, ctype):
        """帶返回導航的 Markdown/純文本頁面。"""
        if not file_path.exists():
            return self.send_json({'error': f'{file_path.name} not found'}, 404)
        body = file_path.read_text(encoding='utf-8', errors='replace')
        escaped = (body.replace('&','&amp;').replace('<','&lt;')
                       .replace('>','&gt;').replace('\n', '<br>'))
        html = (f'<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1">'
                f'<title>{title}</title><style>'
                f'*{{margin:0;padding:0;box-sizing:border-box}}'
                f'body{{font-family:-apple-system,sans-serif;background:#F9F9F6;color:#1F2937;line-height:1.6}}'
                f'header{{background:#FFF;border-bottom:1px solid #E5E7EB;padding:14px 18px;display:flex;align-items:center;gap:10px}}'
                f'header a{{text-decoration:none;color:#0066FF;font-size:14px;font-weight:600}}'
                f'pre{{white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere;font-family:ui-monospace,Menlo,monospace;font-size:13px;line-height:1.6;max-height:70vh;overflow-y:auto;background:#FFF;border:1px solid #E5E7EB;border-radius:12px;padding:14px;margin:18px}}'
                f'</style></head><body><header><a href="/">← 返回主頁</a><h1>{title}</h1></header>'
                f'<pre>{escaped}</pre></body></html>').encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def serve_minutes_html(self, sid):
        """提供紀要 HTML 版（自帶樣式的單檔，供閱讀）。

        檔案不存在時（HTML 功能上線前的舊 session，或產生時失敗），
        即時從 .md 轉一份回傳，**不落盤** —— 不因為有人看了一次就改變既有產出。
        """
        f = MINUTES_DIR / f'{sid}.html'
        if f.exists():
            data = f.read_bytes()
        else:
            md = MINUTES_DIR / f'{sid}.md'
            if not md.exists():
                return self.send_json({'error': 'minutes not found'}, 404)
            try:
                # 延遲 import：server 平常不需要 markdown，也不需要整套 transcribe
                import transcribe as T
                data = T.render_minutes_html(
                    sid, md.read_text(encoding='utf-8')).encode('utf-8')
            except Exception as e:
                return self.send_json(
                    {'error': f'HTML 紀要無法產生：{type(e).__name__}: {e}'}, 500)
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
            'drive_remote': os.environ.get('DRIVE_REMOTE', 'gdrive:RaceOne/Meetings'),
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

        # 即時驗證：壞的 webm（被截斷的最後一片）在這裡就被擋下來，
        # 不會進入 ffmpeg 轉檔、不會產生 mp3、不會進 _list_slices。
        #
        # 2026-11-17 修正（重要 regression）：MediaRecorder 產出的是**串流 WebM**，
        # 合法檔案的 format=duration 就是 "N/A"（串流容器無 duration metadata）。
        # 舊版用 float(duration) 判斷 → float("N/A") 拋 ValueError →
        # **把每一片合法切片都當壞檔拒收**（實測 2026-11-17 整場 34 分鐘會議全滅）。
        # 正確做法：檢查 ffprobe 能讀出音訊串流（codec_type=audio）且 exit 0；
        # 截斷/損毀檔（EBML header parsing failed）ffprobe 會非零退出。
        ok, dur, err = _probe_audio(src)
        if not ok:
            # 2026-11-17 防禦：舊版前端把 MediaRecorder 的第 2 片之後直接切成獨立
            # 檔案（無 EBML header）→ ffprobe 必失敗。這裡自動用同 session 第一片
            # 的 header 補上再試一次，避免「>5 分鐘的會議第 2 片之後全遺失」。
            if _repair_missing_header(src, d):
                ok, dur, err = _probe_audio(src)
                if ok:
                    print(f'[upload] {sid} {filename} 已自動補 EBML header → 通過', flush=True)
        if not ok:
            src.unlink(missing_ok=True)
            print(f'[upload] {sid} {filename} REJECTED: {err}', flush=True)
            return self.send_json({'error': f'invalid audio slice ({err})',
                                   'code': 'INVALID_AUDIO_SLICE',
                                   'verified': False}, 400)

        # 驗證通過才轉 mp3
        mp3 = src.with_suffix('.mp3')
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(src), '-ar', '16000', '-ac', '1',
                        '-c:a', 'libmp3lame', '-b:a', '128k', str(mp3), '-y'],
                       capture_output=True, timeout=120)
        if not mp3.exists():
            src.unlink(missing_ok=True)
            return self.send_json({'error': 'ffmpeg conversion failed'}, 500)

        n = len(list(d.glob('*.mp3')))
        dur_txt = f'{dur:.1f}s' if dur is not None else 'N/A(streaming)'
        print(f'[upload] {sid} {filename} {mp3.stat().st_size}B {dur_txt} slice#{n} verified', flush=True)

        # 遲到的切片：這個 session 已經合併過了，代表這片**不在**逐字稿裡。
        # 不做自動重跑（會產生重複逐字稿），但一定要吵 —— 靜默等於使用者
        # 少了一段會議而沒人知道。
        m = read_meta(sid) or {}
        if m.get('merged_at'):
            now_n = len(_list_slices(d))
            if now_n > int(m.get('slice_count') or 0):
                msg = (f'⚠️ {sid}: 收到遲到的切片 {filename}'
                       f'（現有 {now_n} 片，已合併 {m.get("slice_count")} 片）'
                       f'→ 尾段不在逐字稿裡，請用 /transcribe 重跑')
                print('[late-slice] ' + msg, flush=True)
                write_meta(sid, late_slice_warning=msg,
                           late_slice_at=datetime.now().isoformat())
                notify_telegram(msg)
        self.send_json({'status': 'ok', 'session_id': sid, 'filename': filename,
                        'size': mp3.stat().st_size, 'duration': dur if dur is not None else 0,
                        'slice_index': n, 'verified': True})

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

        # ---- 重複上傳前置檢查 ----
        # 前端帶 fp=<name>|<size>|<lastModified>；同一份檔案在時窗內重複送出，
        # 就沿用既有 session，不重跑轉錄。
        # 注意：**必須先把 body 讀完再回應** —— 若未讀完就回並關閉連線，
        # 客戶端（curl／部分瀏覽器）正在送出的資料會撞上 broken pipe，
        # 使用者看到的是「上傳失敗：網路錯誤」，而不是「這份已上傳過」。
        fp = (q.get('fp', ['']) or [''])[0][:300]
        fp_hit = _index_lookup('fp:' + fp) if fp else None
        if fp_hit:
            self._drain()                     # 先讀完 body 再回，避免客戶端 broken pipe
            prev_sid, _ = fp_hit
            print(f'[upload-meeting] {sid} 重複上傳（fp 命中 {prev_sid}）→ 忽略', flush=True)
            return self.send_json(
                {'status': 'duplicate', 'session_id': prev_sid,
                 'duplicate': True,
                 'note': '這份音檔已上傳過，沿用既有轉錄結果'},
                200)

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

        # ---- 重複上傳後置檢查（權威）----
        # 即使前端沒帶 fp（或連點造成競態），這裡用內容 SHA-256 再擋一次。
        # 命中就直接沿用既有 session，不重跑轉錄、不佔 GPU。
        try:
            sha = _sha256_file(dst)
        except OSError:
            sha = ''
        dup = _index_lookup('sha:' + sha) if sha else None
        if dup:
            prev_sid = dup[0]
            try:
                dst.unlink()                      # 丟掉這份重複檔，不留垃圾
                d.rmdir()                         # 連空的 session 目錄一起收掉
            except OSError:
                pass
            print(f'[upload-meeting] {sid} 重複內容（sha 命中 {prev_sid}）→ 忽略', flush=True)
            return self.send_json(
                {'status': 'duplicate', 'session_id': prev_sid,
                 'duplicate': True,
                 'note': '這份音檔已上傳過，沿用既有轉錄結果'}, 200)

        # 2026-11-17：改用 _probe_audio（與切片上傳一致）。整檔通常有 duration，
        # 但某些串流容器回 N/A 仍屬合法；判定改看「有音訊串流且 ffprobe exit 0」。
        ok, dur, err = _probe_audio(dst)
        if not ok:
            dst.unlink(missing_ok=True)
            print(f'[upload-meeting] {sid} {label} REJECTED: {err}', flush=True)
            return self.send_json({'error': f'invalid audio file ({err})',
                                   'code': 'INVALID_AUDIO_FILE',
                                   'verified': False}, 400)

        dur_txt = f'{dur:.1f}s' if dur is not None else 'N/A(streaming)'
        print(f'[upload-meeting] {sid} {label} {written}B ({written/1048576:.1f} MB) '
              f'dur={dur_txt}', flush=True)

        # 順序很重要：meta（含與會者名單）必須先寫好，再排入轉錄。
        # 否則 worker 可能在名單寫入前就讀到空的 meta。
        write_meta(sid, filename=label, bytes=written,
                   duration=(dur if dur is not None else 0),
                   uploaded_at=datetime.now().isoformat())
        sp = parse_speakers((q.get('speakers', ['']) or [''])[0])
        if sp:
            write_meta(sid, speakers=sp)

        mins_txt = f'{dur/60:.1f}' if dur else '?'
        notify_telegram(f'📥 已收到整檔錄音\nsession: {sid}\n'
                        f'{label} · {written/1048576:.1f} MB · {mins_txt} 分鐘\n'
                        + (f'與會者：{"、".join(sp)}\n' if sp else '')
                        + '\n轉錄中，完成後會再通知你。')

        enqueue_transcription(sid)
        # 登記去重索引（fp 給下次前置檢查、sha 為權威鍵）
        if sha:
            _index_store('sha:' + sha, sid)
        if fp:
            _index_store('fp:' + fp, sid)
        self.send_json({'status': 'accepted', 'session_id': sid, 'filename': label,
                        'bytes': written, 'duration': dur,
                        'speakers': sp, 'queued': True, 'verified': True}, 202)

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
        # 容忍「最後一片還在路上」：等切片出現（最多 END_MEETING_SLICE_WAIT_SEC 秒）
        slices, waited = _wait_for_slices(d, END_MEETING_SLICE_WAIT_SEC)
        if not slices:
            print(f'[end-meeting] {sid} 等了 {waited:.1f}s 仍無切片 → 404',
                  flush=True)
            return self.send_json({'error': 'no slices found',
                                   'waited_sec': waited}, 404)
        if waited:
            print(f'[end-meeting] {sid} 等了 {waited:.1f}s 才看到切片 '
                  f'({len(slices)} 片) → 這是前端「最後一片」競態', flush=True)

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
                   merged_at=datetime.now().isoformat(),
                   slice_names=[p.name for p in slices],
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
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            if self.close_connection:
                self.send_header('Connection', 'close')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 2026-11-17：客戶端已斷線（前端 end-meeting 逾時後放棄連線、
            # 或使用者關閉分頁）。這不是伺服器錯誤，不該噴整段 traceback。
            self.close_connection = True

    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} {args[0]}",
              flush=True)


def _recover_interrupted_fetches():
    """清掉上次執行殘留的「下載中」狀態。

    為什麼需要：分享連結取檔跑在**服務行程的執行緒**裡，服務一重啟
    （部署、改設定、OOM 重拉）就會把下載連根拔起，留下：
      - `fetch_state: downloading` 的假狀態（永遠不會變）
      - 半個 `_incoming*.part` 檔佔著磁碟
    2026-10-01 實際踩到（自己重啟服務時中斷了使用者的取檔）。
    這裡把它標成失敗並清掉半成品，讓使用者能乾淨地重試，
    而不是盯著一個永遠轉不完的「下載中」。
    """
    if not META_DIR.exists():
        return
    n = 0
    for mp in sorted(META_DIR.glob('*.json')):
        sid = mp.stem
        if read_meta(sid).get('fetch_state') != 'downloading':
            continue
        d = UPLOAD_DIR / sid
        if d.exists():
            for f in d.glob('_incoming*'):
                f.unlink(missing_ok=True)
        write_meta(sid, fetch_state='failed',
                   fetch_error='服務重啟中斷取檔，請重新送出連結')
        print(f'[startup] {sid}: 取檔被中斷，已標記失敗並清除半成品', flush=True)
        n += 1
    if n:
        print(f'[startup] 復原 {n} 件被中斷的取檔', flush=True)


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
    _recover_interrupted_fetches()
    # ThreadingHTTPServer + daemon threads：任何單一慢速／卡住的請求只佔用
    # 自己的執行緒，不會讓整個服務停止回應（單執行緒 HTTPServer 會）。
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    srv.daemon_threads = True
    print(f'threading: on（每個連線獨立執行緒）', flush=True)
    srv.serve_forever()
