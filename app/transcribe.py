#!/usr/bin/env python3
"""RaceOne Meeting — 轉錄 worker（faster-whisper，CPU）

用法：
  python3 transcribe.py --worker          處理 queue 內所有待辦（單一實例，flock 保護）
  python3 transcribe.py --session <sid>   只處理指定 session

產出：
  sessions/transcripts/<sid>.txt    人類可讀逐字稿（含時間戳）
  sessions/transcripts/<sid>.json   結構化（segments / language / 時長）
  sessions/minutes/<sid>.md         精簡紀要（OpenRouter；失敗則略過）

實測（本機 1 核 / 1.9GB）：faster-whisper small + int8 + beam1 → RTF ≈ 0.22x、
峰值 RSS ≈ 700–840 MB。故 worker 以 nice 降優先、單一實例執行，避免壓垮其他服務。
"""
import argparse, fcntl, json, os, re, shutil, subprocess, sys, tempfile, time, textwrap, urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(os.environ.get('MEETING_DATA_DIR', '/var/lib/meeting-recorder'))
SESS_DIR = BASE / 'sessions'
UPLOAD_DIR = SESS_DIR / 'uploads'
MERGED_DIR = SESS_DIR / 'merged'
TRANS_DIR = SESS_DIR / 'transcripts'
MINUTES_DIR = SESS_DIR / 'minutes'
QUEUE_DIR = SESS_DIR / 'queue'
META_DIR = SESS_DIR / 'meta'

MODEL_SIZE = os.environ.get('WHISPER_MODEL', 'small')
COMPUTE = os.environ.get('WHISPER_COMPUTE', 'int8')
BEAM = int(os.environ.get('WHISPER_BEAM', '1'))
SUMMARY_MODEL = os.environ.get('SUMMARY_MODEL', 'deepseek/deepseek-v4.1-flash')

# --- Google Drive 歸檔 ---
DRIVE_REMOTE = os.environ.get('DRIVE_REMOTE', 'gdrive:Meetings')
DRIVE_AUDIO = os.environ.get('DRIVE_AUDIO', '1').strip().lower() in ('1', 'true', 'yes', 'on')

# --- 音檔保留 ---
# 只套用在「音檔」上：逐字稿與紀要永久保留（體積小、且是真正的產出）。
RETENTION_DAYS = int(os.environ.get('RETENTION_DAYS', '30'))
TG_CHAT_ID = os.environ.get('MEETING_CHAT_ID', '')   # 無預設：沒設就不推播


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _raise_oom_priority():
    """把自己標成 OOM 時優先犧牲的對象。

    本機只有 1.9 GB，whisper-small 峰值 ~700–840 MB。若發生 global OOM，
    核心會挑 RSS 最大的殺 —— 實測會殺到 Hermes gateway。
    這裡把自己的 oom_score_adj 調高，讓核心優先殺 worker 而不是其他服務。
    （scope unit 不支援 OOMScoreAdjust 屬性，所以在程式內自己寫。）
    """
    try:
        with open('/proc/self/oom_score_adj', 'w') as f:
            f.write('800')
    except OSError:
        pass


def load_env():
    # 環境檔位置由 MEETING_ENV_FILES 指定（冒號分隔），
    # 預設對齊 systemd 佈局 —— 程式碼裡不寫死任何人的路徑。
    for p in [Path(x) for x in os.environ.get(
            'MEETING_ENV_FILES',
            '/etc/meeting-recorder/meeting-recorder.env').split(':')
            if x.strip()]:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------- Telegram ----------------
def tg(method, payload=None, files=None):
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
    if not token:
        log('telegram: 無 token，略過')
        return
    import mimetypes
    if files:
        boundary = '----R1Boundary' + str(int(time.time()))
        body = b''
        for k, v in (payload or {}).items():
            body += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
        for k, path in files.items():
            p = Path(path)
            ct = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; '
                     f'filename="{p.name}"\r\nContent-Type: {ct}\r\n\r\n').encode()
            body += p.read_bytes() + b'\r\n'
        body += f'--{boundary}--\r\n'.encode()
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{token}/{method}', data=body,
            headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    else:
        req = urllib.request.Request(
            f'https://api.telegram.org/bot{token}/{method}',
            data=json.dumps(payload or {}).encode(),
            headers={'Content-Type': 'application/json'})
    try:
        # 逾時刻意壓短：worker 在持有 flock 期間呼叫此函式，
        # 若 Telegram 卡住，整條轉錄佇列都會被鎖住。
        # （2026-10-01 實際遇到 sendDocument 卡住 90 秒以上。）
        urllib.request.urlopen(req, timeout=45).read()
    except Exception as e:
        log(f'telegram {method} 失敗: {e}')


def notify(text, doc=None):
    payload = {'chat_id': TG_CHAT_ID, 'text': text}
    if doc:
        tg('sendDocument', payload=payload, files={'document': doc})
    else:
        tg('sendMessage', payload=payload)


# ---------------- 音訊 ----------------
def to_wav16k(src: Path, dst: Path) -> bool:
    r = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', str(src), '-ar', '16000', '-ac', '1',
         '-c:a', 'pcm_s16le', str(dst), '-y'],
        capture_output=True, text=True, timeout=1800)
    if not dst.exists():
        log(f'ffmpeg 失敗: {r.stderr[-300:]}')
        return False
    return True


def duration_of(path: Path) -> float:
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=nw=1:nk=1', str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


# ---------------- 轉錄 ----------------
# 執行緒數。2026-10-01 實測（真實會議 10 分鐘，small/int8/beam1）：
#   threads=1: RTF 0.168x · 峰值 893 MB  ← 舊值，1 核時代留下來的
#   threads=2: RTF 0.108x · 峰值 1253 MB ← 快 1.56x，本機 2 vCPU 的甜蜜點
#   threads=4: RTF 0.117x · 峰值 1312 MB ← 反而變慢（超額訂閱）
# 注意：2 執行緒峰值 1253 MB 會超過 worker scope 的 MemoryMax，
# 所以 server.py 的 MemoryMax 必須同步調高（現為 1800M）。
THREADS = max(1, int(os.environ.get('WHISPER_THREADS', '2')))


def transcribe(wav: Path):
    from faster_whisper import WhisperModel
    t0 = time.time()
    model = WhisperModel(MODEL_SIZE, device='cpu', compute_type=COMPUTE,
                         cpu_threads=THREADS)
    log(f'model {MODEL_SIZE}/{COMPUTE} 載入 {time.time()-t0:.1f}s '
        f'(cpu_threads={THREADS})')

    t1 = time.time()
    segments, info = model.transcribe(str(wav), beam_size=BEAM, vad_filter=True,
                                      word_timestamps=False)
    out = []
    for s in segments:
        out.append({'start': round(s.start, 2), 'end': round(s.end, 2),
                    'text': s.text.strip()})
    elapsed = time.time() - t1
    rtf = elapsed / info.duration if info.duration else 0
    log(f'轉錄完成 {elapsed:.1f}s / 音訊 {info.duration:.1f}s → RTF {rtf:.2f}x, '
        f'{len(out)} 段, lang={info.language}({info.language_probability:.2f})')
    return out, {'language': info.language,
                 'language_probability': round(info.language_probability, 3),
                 'duration': round(info.duration, 2),
                 'model': f'{MODEL_SIZE}/{COMPUTE}',
                 'beam_size': BEAM,
                 'transcribe_seconds': round(elapsed, 1),
                 'rtf': round(rtf, 3)}


def ts(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f'{h:02d}:{m:02d}:{s:02d}' if h else f'{m:02d}:{s:02d}'


# ---------------- 摘要 ----------------
def openrouter(prompt: str, system: str) -> str:
    key = os.environ.get('OPENROUTER_API_KEY', '')
    if not key:
        return ''
    body = json.dumps({
        'model': SUMMARY_MODEL,
        'messages': [{'role': 'system', 'content': system},
                     {'role': 'user', 'content': prompt}],
        'temperature': 0.2,
    }).encode()
    req = urllib.request.Request(
        'https://openrouter.ai/api/v1/chat/completions', data=body,
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            d = json.loads(r.read())
        return d['choices'][0]['message']['content'].strip()
    except Exception as e:
        log(f'OpenRouter 失敗: {e}')
        return ''


SYS_SUMMARY = ('你是會議記錄助理。根據逐字稿產出精簡紀要，'
               '只根據逐字稿內容，不可臆測或編造，沒有提到的就不要寫。')
SYS_SUMMARY_EN = ('You are a meeting recorder. Produce a concise meeting '
                  'minutes based ONLY on the transcript provided. '
                  'Do not fabricate or guess anything not in the text.')

# 講者歸屬：只做「文字推理」，不是聲紋辨識。因此規則必須極嚴格，
# 寧可留白也不要猜 —— 猜錯的發言歸屬比沒有歸屬更糟。
SYS_SPEAKER = (
    '你是會議記錄助理，負責標註「發言歸屬」。\n'
    '重要限制：你只能依據逐字稿的**文字線索**判斷，你沒有聽過聲音、'
    '沒有做聲紋辨識。因此：\n'
    '- 只有在逐字稿**明確可判定**時才標註發言者，例如：自我介紹、被點名、'
    '  他人以名字稱呼後接著回應、明顯的主持／報告角色。\n'
    '- 只要有一絲不確定，就**不要標註**，寧可留白。\n'
    '- 絕對不可用「好像是」「應該是」的口吻補上人名。\n'
    '- 不可以把同一段話同時歸給多人。\n'
    '若整份逐字稿都沒有足夠線索，直接回答「無足夠線索可判定發言歸屬」。\n'
    '用繁體中文輸出。')

CHUNK_LIMIT = 24000   # 字元


def attribute_speakers(segments, roster) -> str:
    """依名單做「有明確依據才標註」的發言歸屬（非聲紋辨識）。"""
    if not roster:
        return ''
    full = '\n'.join(f'[{ts(s["start"])}] {s["text"]}' for s in segments if s['text'])
    if not full.strip():
        return ''
    names = '、'.join(roster)
    head = (f'已知與會者名單：{names}\n\n'
            f'請列出逐字稿中**能明確判定**發言者的段落，格式為'
            f'「[時間] 姓名：內容摘要」。\n'
            f'若某位與會者完全沒有可判定的發言，註明「無可判定發言」。\n'
            f'結尾加上一句「以上為文字推理，非聲紋辨識」的提醒。\n\n逐字稿：\n')
    # 長稿只取前後段，避免爆 token；歸屬本來就是輔助資訊
    body = full if len(full) <= CHUNK_LIMIT else full[:CHUNK_LIMIT]
    return openrouter(head + body, SYS_SPEAKER)


def summarize(segments, lang='zh') -> str:
    """產出單語摘要（繁中 or 英文），依 lang 參數切換。"""
    full = '\n'.join(s['text'] for s in segments if s['text'])
    if not full.strip():
        return ''

    system = SYS_SUMMARY if lang == 'zh' else SYS_SUMMARY_EN
    # 標題模板：{part} 佔位，只有長逐字稿切塊時才替換
    head_zh = ('以下是一場會議的逐字稿{part}。請產出：\n'
               '1. **一句話總結**\n2. **主要討論重點**（列點）\n'
               '3. **決議事項**\n4. **行動項目**（負責人／期限，未提及寫「未指定」）\n'
               '5. **待解問題**\n\n逐字稿：\n')
    head_en = ('Here is a meeting transcript{part}.\nPlease produce:\n'
               '1. **One-sentence summary**\n'
               '2. **Key discussion points**\n3. **Decisions**\n'
               '4. **Action items** (owner/deadline, or "TBD")\n'
               '5. **Open questions**\n\nTranscript:\n')
    head = head_zh if lang == 'zh' else head_en

    def one(text, part=None):
        tag = ''
        if part:
            tag = f'（第 {part} 部分）' if lang == 'zh' else f' (Part {part})'
        return openrouter(head.replace('{part}', tag) + text, system)

    if len(full) <= CHUNK_LIMIT:
        return one(full)

    # 長逐字稿：切塊摘要後再彙整
    parts, cur = [], ''
    for line in full.split('\n'):
        if len(cur) + len(line) > CHUNK_LIMIT:
            parts.append(cur); cur = ''
        cur += line + '\n'
    if cur:
        parts.append(cur)

    partials = []
    for i, p in enumerate(parts, 1):
        r = one(p, i)
        if r:
            partials.append(r)
    if not partials:
        return ''
    if len(partials) == 1:
        return partials[0]
    merged = '\n\n---\n\n'.join(partials)
    merge_prompt = ('以下是同一場會議各段落的摘要，請合併成一份完整紀要，'
                    '去除重複、保留所有決議與行動項目：\n\n' + merged)
    merge_prompt_en = ('The following are summaries of parts of the same meeting.'
                       ' Merge them into one coherent minutes, remove duplicates,'
                       ' keep all decisions and action items:\n\n' + merged)
    return openrouter(merge_prompt if lang == 'zh' else merge_prompt_en, system)


SYS_TRANSLATE = ('你是專業會議紀錄翻譯。只翻譯使用者提供的內容，'
                 '不增刪、不加解釋、不評論，保持原本的條列與標題結構。')
SYS_TRANSLATE_EN = ('You are a professional minutes translator. Translate only, '
                    'do not add, remove, explain or comment. '
                    'Preserve the original structure and bullet formatting.')


def summarize_bilingual(segments, lang='zh') -> tuple:
    """回傳 (zh_minutes, en_minutes)。

    lang='zh' → 先產中文紀要，再翻譯成英文。
    lang='en' → 先產英文紀要，再翻譯成中文。
    翻譯失敗時，盡量不讓已產生的那一邊白費。
    """
    primary = summarize(segments, lang)
    if not primary:
        return '', ''

    if lang == 'zh':
        prompt = ('將以下會議紀要翻譯成英文（English）。\n\n' + primary)
        secondary = openrouter(prompt, SYS_TRANSLATE_EN)
        return primary, secondary
    else:
        prompt = ('將以下會議紀要翻譯成繁體中文。\n\n' + primary)
        secondary = openrouter(prompt, SYS_TRANSLATE)
        return secondary, primary


def clean_minutes(text: str) -> str:
    """移除 LLM 自己在開頭加的一或多層標題。

    模型常會回「## 會議紀要」「**Meeting Minutes**」之類的抬頭，我們在外層
    已經有 `## 中文紀要` / `## English Minutes`，重複會變成雙重標題。
    """
    lines = text.strip().splitlines()
    while lines:
        s = lines[0].strip()
        if not s or re.match(r'^#{1,6}\s', s) or re.fullmatch(r'\*\*[^*]{1,40}\*\*', s):
            lines.pop(0)
            continue
        break
    return '\n'.join(lines).strip()


def detect_lang(meta: dict) -> str:
    """根據 meta 語言偵測結果回傳 'zh' 或 'en'。"""
    lang = meta.get('language', 'zh')
    prob = meta.get('language_probability', 0)
    if lang in ('en', 'zh') and prob >= 0.4:
        return lang
    # 低置信度預設中文
    return 'zh'


# ---------------- session meta（與會者名單等）----------------
def meta_path(sid: str) -> Path:
    return META_DIR / f'{sid}.json'


def read_meta(sid: str) -> dict:
    try:
        return json.loads(meta_path(sid).read_text(encoding='utf-8'))
    except Exception:
        return {}


def write_meta(sid: str, **kw) -> dict:
    META_DIR.mkdir(parents=True, exist_ok=True)
    m = read_meta(sid)
    m.update(kw)
    meta_path(sid).write_text(json.dumps(m, ensure_ascii=False, indent=2),
                              encoding='utf-8')
    return m


# ---------------- Google Drive 歸檔 ----------------
def archive_to_drive(sid: str) -> tuple:
    """把逐字稿／紀要／（選配）音檔歸檔到 Google Drive。

    回傳 (drive_path, error)。任何失敗都不得讓轉錄本身失敗 ——
    本機檔案才是主要產出，Drive 是備份。
    """
    if not shutil.which('rclone'):
        return None, 'rclone 不存在'

    ym = datetime.now().strftime('%Y-%m')
    dest = f'{DRIVE_REMOTE}/{ym}/{sid}'

    files = [TRANS_DIR / f'{sid}.txt', TRANS_DIR / f'{sid}.json']
    for extra in (MINUTES_DIR / f'{sid}.md', TRANS_DIR / f'{sid}.speakers.txt'):
        if extra.exists():
            files.append(extra)
    audio = find_audio(sid)
    if DRIVE_AUDIO and audio:
        files.append(audio)
    files = [f for f in files if f.exists()]
    if not files:
        return None, '沒有可歸檔的檔案'

    staging = Path(tempfile.mkdtemp(prefix=f'r1drv-{sid}-'))
    try:
        for f in files:
            shutil.copy2(f, staging / f.name)

        # 一次 copy 整個目錄，而不是每個檔案各發一次 API call
        r = subprocess.run(
            ['rclone', 'copy', str(staging), dest,
             '--transfers', '2', '--retries', '3', '--low-level-retries', '5',
             '--retries-sleep', '5s'],
            capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            lines = [ln.strip() for ln in (r.stderr or r.stdout or '').splitlines()
                     if ln.strip()]
            return None, (lines[-1] if lines else f'rclone exit {r.returncode}')
        return dest, None
    except subprocess.TimeoutExpired:
        return None, 'rclone 逾時'
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# ---------------- Drive 歸檔重試 ----------------
def retry_archives() -> tuple:
    """重試先前歸檔失敗的 session。

    Google Drive 會回 rateLimitExceeded（實測為暫時性，稍後即成功）。
    歸檔失敗時我們在 meta 標記 drive_pending，這裡補做。
    回傳 (成功數, 仍失敗數)。
    """
    if not META_DIR.exists():
        return 0, 0
    ok = fail = 0
    for mp in sorted(META_DIR.glob('*.json')):
        sid = mp.stem
        m = read_meta(sid)
        if not m.get('drive_pending'):
            continue
        if not (TRANS_DIR / f'{sid}.txt').exists():
            continue                      # 還沒轉錄完，還輪不到歸檔
        path, err = archive_to_drive(sid)
        if path:
            write_meta(sid, drive_path=path, drive_pending=False, drive_error=None)
            log(f'{sid}: 補歸檔成功 → {path}')
            ok += 1
        else:
            write_meta(sid, drive_error=err)
            log(f'{sid}: 補歸檔仍失敗 — {err}')
            fail += 1
    return ok, fail


# ---------------- 音檔保留政策 ----------------
def cleanup(days: int = None, dry_run: bool = False) -> tuple:
    """刪除超過保留期的音檔（uploads/、merged/）。

    逐字稿、紀要、meta **永不刪除** —— 體積小且是真正的產出。
    回傳 (檔案清單, 總位元組)。
    """
    days = RETENTION_DAYS if days is None else days
    cutoff = time.time() - days * 86400
    hit, total = [], 0

    for root in (UPLOAD_DIR, MERGED_DIR):
        if not root.exists():
            continue
        for p in sorted(root.rglob('*')):
            try:
                if not p.is_file() or p.stat().st_mtime >= cutoff:
                    continue
                size = p.stat().st_size
            except OSError:
                continue
            hit.append((p, size))
            total += size
            if not dry_run:
                p.unlink(missing_ok=True)

    if not dry_run:
        # 清掉空的 session 目錄
        for d in sorted(UPLOAD_DIR.glob('*'), reverse=True):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    return hit, total


# ---------------- 單一 session ----------------
def find_audio(sid: str):
    merged = MERGED_DIR / f'{sid}.mp3'
    if merged.exists():
        return merged
    d = UPLOAD_DIR / sid
    if d.exists():
        cands = sorted(d.glob('*.mp3')) + sorted(d.glob('*.m4a')) + \
                sorted(d.glob('*.webm')) + sorted(d.glob('*.wav'))
        if cands:
            return cands[0]
    return None


def process(sid: str) -> bool:
    src = find_audio(sid)
    if not src:
        log(f'{sid}: 找不到音訊，略過')
        return False

    TRANS_DIR.mkdir(parents=True, exist_ok=True)
    MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    wav = TRANS_DIR / f'{sid}.16k.wav'

    log(f'{sid}: 來源 {src.name} ({src.stat().st_size/1048576:.1f} MB), '
        f'音訊 {duration_of(src):.0f}s')
    if not to_wav16k(src, wav):
        notify(f'❌ 轉錄失敗（音訊解碼）\nsession: {sid}')
        return False

    try:
        segments, meta = transcribe(wav)
    except Exception as e:
        log(f'{sid}: 轉錄錯誤 {type(e).__name__}: {e}')
        notify(f'❌ 轉錄失敗\nsession: {sid}\n{type(e).__name__}: {e}')
        return False

    txt = TRANS_DIR / f'{sid}.txt'
    txt.write_text(
        f'# 逐字稿 {sid}\n'
        f'語言: {meta["language"]} ({meta["language_probability"]})  '
        f'時長: {ts(meta["duration"])}  '
        f'模型: {meta["model"]}\n\n' +
        '\n'.join(f'[{ts(s["start"])}] {s["text"]}' for s in segments if s['text']) + '\n',
        encoding='utf-8')
    (TRANS_DIR / f'{sid}.json').write_text(
        json.dumps({'session_id': sid, 'meta': meta, 'segments': segments},
                   ensure_ascii=False, indent=2), encoding='utf-8')

    log(f'{sid}: 逐字稿 {len(segments)} 段 → {txt.name}')

    info = read_meta(sid)
    roster = [s.strip() for s in (info.get('speakers') or []) if s and s.strip()]

    lang = detect_lang(meta)
    zh_min, en_min = summarize_bilingual(segments, lang)

    attribution = attribute_speakers(segments, roster) if roster else ''

    md = None
    if zh_min or en_min or attribution:
        blk = [f'# 會議紀要 {sid}', '']
        if info.get('filename'):
            blk.append(f'來源音檔：{info["filename"]}')
        if roster:
            blk.append(f'與會者名單：{"、".join(roster)}')
        blk.append('')
        if zh_min:
            blk.append('## 中文紀要')
            blk.append(clean_minutes(zh_min))
        if en_min:
            blk += ['', '## English Minutes', '', clean_minutes(en_min)]
        if attribution:
            blk += ['', '## 發言歸屬（文字推理，非聲紋辨識）', '', attribution]
        md = MINUTES_DIR / f'{sid}.md'
        md.write_text('\n'.join(blk) + '\n', encoding='utf-8')
        log(f'{sid}: 紀要已產生（雙語）' + ('（含發言歸屬）' if attribution else ''))
    else:
        log(f'{sid}: 紀要未產生（OpenRouter 無回應）')

    if attribution:
        (TRANS_DIR / f'{sid}.speakers.txt').write_text(
            f'# 發言歸屬 {sid}（文字推理，非聲紋辨識）\n'
            f'與會者名單：{"、".join(roster)}\n\n{attribution}\n', encoding='utf-8')

    wav.unlink(missing_ok=True)   # 16k wav 只是中間產物，省磁碟

    # ---- Google Drive 歸檔（失敗不得影響本機產出）----
    drive_path, derr = archive_to_drive(sid)
    if drive_path:
        write_meta(sid, drive_path=drive_path, archived_at=datetime.now().isoformat(),
                   drive_pending=False, drive_error=None)
        log(f'{sid}: 已歸檔至 {drive_path}')
    else:
        # 標記待補：Drive 的 rateLimitExceeded 是暫時性的，稍後重試即可
        write_meta(sid, drive_pending=True, drive_error=derr)
        log(f'{sid}: Drive 歸檔未完成 — {derr}（已標記待補歸檔）')

    head_zh = (zh_min[:1200] + '\n\n（完整紀要見附件）') if zh_min and len(zh_min) > 1200 \
        else (zh_min or '（未產生紀要）')
    head_en = (en_min[:1200] + '\n\n（Full minutes in attachment）') if en_min and len(en_min) > 1200 \
        else (en_min or '(no minutes generated)')
    drive_line = (f'\n📁 Drive: {drive_path}' if drive_path
                  else f'\n⚠️ Drive 歸檔失敗：{derr}')
    notify(f'✅ 轉錄完成 / Transcription done\n'
           f'session: {sid}\n'
           f'時長 {ts(meta["duration"])} · {len(segments)} 段 · '
           f'{meta["language"]} · 耗時 {meta["transcribe_seconds"]:.0f}s\n'
           f'{drive_line}\n\n'
           f'中文：\n{head_zh}\n\n'
           f'English:\n{head_en}',
           doc=str(txt))
    return True


# ---------------- queue worker ----------------
def worker():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = QUEUE_DIR / '.lock'
    lf = open(lock_path, 'w')
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log('已有 worker 在跑，結束')
        return 0

    done = 0
    while True:
        jobs = sorted(QUEUE_DIR.glob('*.json'))
        if not jobs:
            # 關閉競態：新工作可能在我們掃描前一刻才寫入，且對應的 worker
            # 因為搶不到鎖而直接結束。這裡再等一下子重掃一次，避免工作被遺漏。
            time.sleep(3)
            jobs = sorted(QUEUE_DIR.glob('*.json'))
            if not jobs:
                break

        for job in jobs:
            try:
                data = json.loads(job.read_text())
            except Exception:
                data = {'session_id': job.stem, 'attempts': 0}
            sid = data.get('session_id', job.stem)
            data['attempts'] = int(data.get('attempts', 0)) + 1

            ok = process(sid)
            if ok:
                done += 1
                job.unlink(missing_ok=True)
            elif data['attempts'] >= 2:
                # 重試上限：避免 worker 被 OOM 或崩潰後無限重跑同一件
                log(f'{sid}: 已嘗試 {data["attempts"]} 次仍失敗，放棄')
                notify(f'❌ 轉錄失敗（已重試 {data["attempts"]} 次）\nsession: {sid}\n'
                       f'音檔仍在伺服器上，可用「重新轉錄」再試。')
                (QUEUE_DIR / f'{sid}.failed').write_text(
                    json.dumps(data, ensure_ascii=False))
                job.unlink(missing_ok=True)
            else:
                data['last_attempt'] = datetime.now().isoformat()
                job.write_text(json.dumps(data, ensure_ascii=False))
                log(f'{sid}: 失敗，保留待重試（第 {data["attempts"]} 次）')
    log(f'worker 結束，處理 {done} 件')
    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--worker', action='store_true')
    ap.add_argument('--session')
    ap.add_argument('--cleanup', action='store_true',
                    help='刪除超過保留期的音檔（逐字稿/紀要永不刪）')
    ap.add_argument('--days', type=int, default=None,
                    help=f'保留天數（預設 {RETENTION_DAYS}）')
    ap.add_argument('--dry-run', action='store_true', help='只列出不刪除')
    ap.add_argument('--retry-archives', action='store_true',
                    help='重試先前失敗的 Google Drive 歸檔')
    a = ap.parse_args()
    _raise_oom_priority()
    load_env()
    for d in (UPLOAD_DIR, MERGED_DIR, TRANS_DIR, MINUTES_DIR, QUEUE_DIR, META_DIR):
        d.mkdir(parents=True, exist_ok=True)

    if a.cleanup:
        days = RETENTION_DAYS if a.days is None else a.days
        hits, total = cleanup(days=days, dry_run=a.dry_run)
        verb = '將刪除' if a.dry_run else '已刪除'
        log(f'保留政策 {days} 天：{verb} {len(hits)} 個音檔，'
            f'共 {total/1048576:.1f} MB')
        for p, sz in hits:
            log(f'  {"[dry] " if a.dry_run else ""}{p} ({sz/1024:.0f} KB)')
        sys.exit(0)

    if a.retry_archives:
        ok, fail = retry_archives()
        log(f'補歸檔：成功 {ok} 件，仍失敗 {fail} 件')
        sys.exit(0)

    if a.session:
        sys.exit(0 if process(a.session) else 1)
    sys.exit(worker())
