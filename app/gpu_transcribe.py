#!/usr/bin/env python3
"""用 Runpod serverless GPU 轉錄（WhisperX large-v2），取代本機 whisper。

為什麼要做這個：
  本機 CPU（2 vCPU）RTF ≈ 0.12x → 8 小時會議約 52 分鐘。
  Runpod GPU 實測 RTF 0.022x → 約 11 分鐘，而且模型由 small 升級為 large-v2。

大檔怎麼送（這是重點）：
  Runpod `/run` 走 base64 的 payload 上限約 10 MB，而 8 小時的 16k wav 約 900 MB。
  理論上可以給公開 URL 讓 worker 自己抓，但**會議音檔不可能公開**（隱私）。
  所以切成多塊並行送，再把各塊 segments 依時間偏移合併。

為什麼要重疊（overlap）：
  硬切會把字切斷。每塊前後各多抓 15 秒，合併時只保留「中點落在該塊擁有區間」
  的 segments —— 邊界上的字至少會落在某一塊的擁有區間內，不會漏也不會重複。

輸出與 transcribe.py 完全一致（transcripts/<sid>.{txt,json} + 雙語紀要 + Drive 歸檔），
所以前端、/status、/minutes 都不用改。

用法：
  venv/bin/python gpu_transcribe.py <sid>
  venv/bin/python gpu_transcribe.py <sid> --keep-chunks   # 保留切塊供除錯
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcribe as T                      # 重用逐字稿格式／紀要／歸檔邏輯

# 沒有預設值：每個人的 endpoint 不同，寫死別人的只會誤導。
ENDPOINT = os.environ.get('RUNPOD_ENDPOINT_ID', '')
BASE = f'https://api.runpod.ai/v2/{ENDPOINT}'

CHUNK_SEC = int(os.environ.get('GPU_CHUNK_SEC', '600'))   # 每塊 10 分鐘
OVERLAP_SEC = int(os.environ.get('GPU_OVERLAP_SEC', '15'))
MP3_BITRATE = '64k'          # 64k mono mp3：10 分鐘約 4.8 MB，base64 後約 6.4 MB
PARALLEL = int(os.environ.get('GPU_PARALLEL', '3'))       # 對齊 endpoint workersMax
POLL_SEC = 6
POLL_MAX = int(os.environ.get('GPU_POLL_MAX', '400'))     # 有界輪詢
SUBMIT_RETRY = 2


def api_key():
    """從環境或 profile .env 取得金鑰（worker 由 systemd 啟動時環境可能沒有）。"""
    k = os.environ.get('RUNPOD_API_KEY')
    if k:
        return k
    for p in [Path(x) for x in os.environ.get(
            'MEETING_ENV_FILES',
            '/etc/meeting-recorder/meeting-recorder.env').split(':') if x.strip()]:
        if p.exists():
            for ln in p.read_text(encoding='utf-8', errors='replace').splitlines():
                if ln.strip().startswith('RUNPOD_API_KEY'):
                    return ln.split('=', 1)[1].strip().strip('"\'')
    raise RuntimeError('找不到 RUNPOD_API_KEY')


KEY = None          # 延後取得：沒設金鑰時仍可 import 與讀 --help


def _key():
    global KEY
    if KEY is None:
        KEY = api_key()
    return KEY


def call(path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={'Authorization': f'Bearer {_key()}',
                                          'Content-Type': 'application/json',
                                          'User-Agent': 'raceone-gpu/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return None, f'{type(e).__name__}: {e}'


def make_chunks(wav: Path, outdir: Path, total: float):
    """切成 mp3 小塊（含前後 overlap）。回傳 [(index, owned_start, owned_end, path)]。"""
    outdir.mkdir(parents=True, exist_ok=True)
    chunks = []
    idx, start = 0, 0.0
    while start < total:
        owned_end = min(start + CHUNK_SEC, total)
        cut_start = max(0.0, start - OVERLAP_SEC)
        cut_end = min(total, owned_end + OVERLAP_SEC)
        dst = outdir / f'chunk_{idx:04d}.mp3'
        T.log(f'  切塊 {idx:04d}: 擁有 {start:.0f}-{owned_end:.0f}s '
              f'(實際切 {cut_start:.0f}-{cut_end:.0f}s)')
        r = os.system(
            f'ffmpeg -y -v error -ss {cut_start:.3f} -t {cut_end-cut_start:.3f} '
            f'-i "{wav}" -ac 1 -ar 16000 -b:a {MP3_BITRATE} "{dst}"')
        if r != 0 or not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError(f'ffmpeg 切塊失敗（chunk {idx}）')
        chunks.append({'i': idx, 'owned_start': start, 'owned_end': owned_end,
                       'cut_start': cut_start, 'path': dst})
        idx += 1
        start = owned_end
    return chunks


def submit_all(chunks):
    """全部一次送出 —— 讓佇列餵飽 worker，避免 worker 閒置後又冷啟。"""
    def submit(c):
        b = c['path'].read_bytes()
        payload = {'input': {
            'audio_file': 'data:audio/mp3;base64,' + base64.b64encode(b).decode(),
            'align_output': False,
        }}
        for attempt in range(SUBMIT_RETRY + 1):
            st, d = call('/run', payload)
            if isinstance(d, dict) and d.get('id'):
                return c['i'], d['id'], None
            if attempt < SUBMIT_RETRY:
                time.sleep(3)
        return c['i'], None, f'{st} {str(d)[:150]}'

    with ThreadPoolExecutor(max_workers=PARALLEL) as ex:
        for i, jid, err in ex.map(submit, chunks):
            if err:
                T.log(f'  塊 {i:04d} 送件失敗：{err}')
            yield i, jid, err


def wait_job(jid):
    for _ in range(POLL_MAX):
        time.sleep(POLL_SEC)
        st, s = call(f'/status/{jid}')
        if isinstance(s, dict) and s.get('status') in (
                'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'):
            return s
    return {'status': 'TIMED_OUT', 'id': jid}


def gpu_transcribe(wav: Path, workdir: Path):
    total = T.duration_of(wav)
    chunks = make_chunks(wav, workdir, total)

    T.log(f'共 {len(chunks)} 塊，並行 {PARALLEL}，總長 {total:.0f}s '
          f'→ 全部送出')
    jobs = {}
    for i, jid, err in submit_all(chunks):
        if jid:
            jobs[i] = jid

    results, langs = {}, {}
    for i, jid in sorted(jobs.items()):
        s = wait_job(jid)
        if s.get('status') != 'COMPLETED':
            T.log(f'  塊 {i:04d} {s.get("status")} —— 失敗')
            results[i] = None
            continue
        out = s.get('output') or {}
        results[i] = out.get('segments') or []
        langs[i] = out.get('detected_language')
        T.log(f'  塊 {i:04d} 完成：{len(results[i])} 段 · '
              f'{out.get("detected_language")} · '
              f'執行 {s.get("executionTime", 0)/1000:.1f}s')

    missing = [c['i'] for c in chunks if not results.get(c['i'])]
    if missing:
        raise RuntimeError(f'有 {len(missing)} 塊失敗：{missing[:5]}')

    # 合併：只保留中點落在該塊「擁有區間」的 segments，避免重疊區重複
    merged = []
    for c in chunks:
        off = c['cut_start']
        for seg in results[c['i']]:
            st_ = seg.get('start', 0) + off
            en_ = seg.get('end', st_) + off
            mid = (st_ + en_) / 2
            if c['owned_start'] <= mid < c['owned_end'] or \
               (c['i'] == len(chunks) - 1 and mid >= c['owned_start']):
                txt = (seg.get('text') or '').strip()
                if txt:
                    merged.append({'start': round(st_, 2),
                                   'end': round(en_, 2), 'text': txt})
    merged.sort(key=lambda s: s['start'])

    # 主要語言：取最常出現的偵測結果
    lang = 'en'
    if langs:
        from collections import Counter
        lang = Counter(v for v in langs.values() if v).most_common(1)[0][0]

    meta = {'language': lang, 'language_probability': 1.0,
            'duration': round(total, 2),
            'model': f'whisperx-large-v2@gpu:{ENDPOINT}',
            'chunks': len(chunks), 'rtf_source': 'runpod-serverless'}
    return merged, meta


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    # 必須先載入環境變數（OPENROUTER_API_KEY 等）。
    # 為什麼容易漏：`load_env()` 只在 transcribe.py 的 __main__ 被呼叫，
    # 直接 import transcribe **不會**觸發。漏掉的症狀是紀要**靜默消失** ——
    # openrouter() 在金鑰為空時直接回傳 ''，不報錯，只留逐字稿沒有紀要。
    T.load_env()

    sid = sys.argv[1]
    keep = '--keep-chunks' in sys.argv

    if not ENDPOINT:
        print('錯誤：請設定 RUNPOD_ENDPOINT_ID（Runpod serverless endpoint id）')
        return 2

    src = T.find_audio(sid)
    if not src:
        T.log(f'{sid}: 找不到音訊')
        return 1

    T.TRANS_DIR.mkdir(parents=True, exist_ok=True)
    workdir = T.TRANS_DIR / f'{sid}.chunks'
    wav = T.TRANS_DIR / f'{sid}.16k.wav'

    T.log(f'{sid}: 來源 {src.name} ({src.stat().st_size/1048576:.1f} MB) → GPU')
    t0 = time.time()
    if not T.to_wav16k(src, wav):
        T.notify(f'❌ GPU 轉錄失敗（音訊解碼）\nsession: {sid}')
        return 1

    try:
        segments, meta = gpu_transcribe(wav, workdir)
    except Exception as e:
        T.log(f'{sid}: GPU 轉錄錯誤 {type(e).__name__}: {e}')
        T.notify(f'❌ GPU 轉錄失敗\nsession: {sid}\n{type(e).__name__}: {e}')
        return 1
    finally:
        wav.unlink(missing_ok=True)
        if not keep:
            for f in workdir.glob('*'):
                f.unlink(missing_ok=True)
            try:
                workdir.rmdir()
            except OSError:
                pass

    elapsed = time.time() - t0
    T.log(f'{sid}: GPU 轉錄 {len(segments)} 段 · 總耗時 {elapsed:.1f}s · '
          f'RTF {elapsed/meta["duration"]:.4f}x')

    # 逐字稿（格式與本機管線一致）
    txt = T.TRANS_DIR / f'{sid}.txt'
    txt.write_text(
        f'# 逐字稿 {sid}\n'
        f'語言: {meta["language"]} ({meta["language_probability"]})  '
        f'時長: {T.ts(meta["duration"])}  '
        f'模型: {meta["model"]}\n\n' +
        '\n'.join(f'[{T.ts(s["start"])}] {s["text"]}' for s in segments) + '\n',
        encoding='utf-8')
    (T.TRANS_DIR / f'{sid}.json').write_text(
        json.dumps({'session_id': sid, 'meta': meta, 'segments': segments},
                   ensure_ascii=False, indent=2), encoding='utf-8')
    T.log(f'{sid}: 逐字稿 {len(segments)} 段 → {txt.name}')

    # 雙語紀要
    info = T.read_meta(sid)
    roster = [s.strip() for s in (info.get('speakers') or []) if s and s.strip()]
    lang = T.detect_lang(meta)
    zh_min, en_min = T.summarize_bilingual(segments, lang)
    attribution = T.attribute_speakers(segments, roster) if roster else ''

    if zh_min or en_min or attribution:
        blk = [f'# 會議紀要 {sid}', '']
        if info.get('filename'):
            blk.append(f'來源音檔：{info["filename"]}')
        if roster:
            blk.append(f'與會者名單：{"、".join(roster)}')
        blk.append('')
        if zh_min:
            blk += ['## 中文紀要', T.clean_minutes(zh_min)]
        if en_min:
            blk += ['', '## English Minutes', '', T.clean_minutes(en_min)]
        if attribution:
            blk += ['', '## 發言歸屬（文字推理，非聲紋辨識）', '', attribution]
        (T.MINUTES_DIR / f'{sid}.md').write_text('\n'.join(blk) + '\n',
                                                 encoding='utf-8')
        T.log(f'{sid}: 紀要已產生（雙語）')

    if attribution:
        (T.TRANS_DIR / f'{sid}.speakers.txt').write_text(
            f'# 發言歸屬 {sid}（文字推理，非聲紋辨識）\n'
            f'與會者名單：{"、".join(roster)}\n\n{attribution}\n', encoding='utf-8')

    drive_path, derr = T.archive_to_drive(sid)
    if drive_path:
        # 原本這裡只印日誌、不寫 meta，導致前端 archived 永遠是 False。
        # 與本機路徑（transcribe.process）寫入的欄位保持一致。
        T.write_meta(sid, drive_path=drive_path,
                     archived_at=T.datetime.now().isoformat(),
                     drive_pending=False, drive_error=None)
        T.log(f'{sid}: Drive 歸檔 → {drive_path}')
        # 備份確認存在了，本機音檔可以放掉（逐字稿／紀要／meta 不動）
        T.maybe_purge_audio(sid, drive_path)
    else:
        T.write_meta(sid, drive_pending=True, drive_error=derr)
        T.log(f'{sid}: Drive 歸檔未完成 — {derr}（已標記待補歸檔，保留音檔）')

    T.notify(f'✅ GPU 轉錄完成\nsession: {sid}\n{len(segments)} 段 · '
             f'{elapsed/60:.1f} 分鐘（RTF {elapsed/meta["duration"]:.3f}x）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
