#!/usr/bin/env python3
"""End-to-end verification of the meeting recorder, over public HTTPS."""
import json, os, subprocess, time, urllib.request, urllib.error
from pathlib import Path

TOKEN = [l.split('=', 1)[1].strip() for l in
         Path(os.environ.get('MEETING_ENV_FILES',
                             '/etc/meeting-recorder/meeting-recorder.env')
              .split(':')[0]).read_text().splitlines()
         if l.startswith('MEETING_TOKEN=')][0]
BASE = os.environ.get('MEETING_BASE_URL', 'https://localhost:8765')
SAMPLE = os.environ.get('MEETING_SAMPLE_AUDIO', './sample.mp3')

results = []


def check(name, ok, detail=''):
    results.append((name, ok, detail))
    print(f"{'✅' if ok else '❌'} {name}" + (f"  — {detail}" if detail else ''))


def req(path, data=None, method='GET', ctype=None, token=True, timeout=180):
    h = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
                       'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1'}
    if token:
        h['X-Meeting-Token'] = TOKEN
    if ctype:
        h['Content-Type'] = ctype
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


# 1. health
s, b = req('/health', token=False)
d = json.loads(b) if s == 200 else {}
check('GET /health 公開可用', s == 200 and d.get('status') == 'ok', f'HTTP {s} queue={d.get("queue")}')

# 2. PWA shell
s, b = req('/', token=False)
check('GET / 提供 PWA 外殼', s == 200 and '上傳音檔'.encode() in b, f'HTTP {s} {len(b)}B')

# 3. auth gate
s, _ = req('/upload-meeting?session_id=x&name=a.m4a', data=b'x',
           method='POST', ctype='audio/mpeg', token=False)
check('未帶權杖上傳 → 401', s == 401, f'HTTP {s}')

# 4. bad json
s, _ = req('/end-meeting', data=b'{}', method='POST', ctype='application/json')
check('end-meeting 缺 session_id → 400', s == 400, f'HTTP {s}')

# 5. upload a whole file + participants
sid = 'e2e_' + time.strftime('%H%M%S')
audio = Path(SAMPLE).read_bytes()
s, b = req(f'/upload-meeting?session_id={sid}&name=%E8%AA%9E%E9%9F%B3%E5%82%99%E5%BF%98%E9%8C%84.m4a'
           f'&speakers=Alice,Bob',
           data=audio, method='POST', ctype='audio/mpeg')
up = json.loads(b) if s < 300 else {}
check('上傳整檔（含與會者）→ 202 並排入轉錄', s == 202 and up.get('queued'),
      f'HTTP {s} speakers={up.get("speakers")}')

s, b = req(f'/meta/{sid}')
meta = json.loads(b) if s == 200 else {}
check('與會者名單已寫入 meta', meta.get('speakers') == ['Alice', 'Bob'],
      f'{meta.get("speakers")}')

t0 = time.time()
txt = None
while time.time() - t0 < 300:
    s, b = req(f'/transcript/{sid}')
    if s == 200:
        txt = b.decode()
        break
    time.sleep(5)
ok = txt is not None
check('轉錄完成並可取用逐字稿', ok, f'{time.time()-t0:.0f}s' if ok else '逾時')

if ok:
    s, b = req(f'/transcript/{sid}?format=json')
    meta = json.loads(b)['meta'] if s == 200 else {}
    check('結構化 JSON', s == 200 and 'segments' in json.loads(b),
          f'lang={meta.get("language")} rtf={meta.get("rtf")} segs={len(json.loads(b)["segments"])}')

    s, b = req(f'/status/{sid}')
    st = json.loads(b) if s == 200 else {}
    check('GET /status 回報完成狀態', s == 200 and st.get('transcript'),
          f'transcript={st.get("transcript")} minutes={st.get("minutes")} done={st.get("done")}')

    # 紀要比逐字稿晚出現（還要呼叫 LLM）→ 必須輪詢，不可立刻抓
    t1 = time.time()
    mok = False
    while time.time() - t1 < 180:
        s, b = req(f'/minutes/{sid}')
        if s == 200:
            mok = True
            break
        time.sleep(5)
    check('會議紀要可取得（輪詢後）', mok,
          f'{time.time()-t1:.0f}s' if mok else '逾時')

    # 講者標註：有填名單就應產出（內容正確性由 prompt 把關，這裡驗檔案存在）
    s, b = req(f'/speakers/{sid}')
    check('發言歸屬檔案可取得', s == 200 and '文字推理'.encode() in b,
          f'HTTP {s} {len(b)}B')

    # Drive 歸檔：發生在紀要之後（rclone），必須輪詢；成功要有路徑，
    # 失敗則必須標記為待補 —— 不可靜默失敗。
    t2 = time.time()
    dst = {}
    while time.time() - t2 < 120:
        s, b = req(f'/status/{sid}')
        dst = json.loads(b) if s == 200 else {}
        if dst.get('drive_path') or dst.get('drive_pending'):
            break
        time.sleep(5)
    check('Drive 歸檔有結果（成功或標記待補）',
          bool(dst.get('drive_path')) or dst.get('drive_pending'),
          f'archived={dst.get("archived")} path={dst.get("drive_path")} '
          f'pending={dst.get("drive_pending")} ({time.time()-t2:.0f}s)')

s, b = req('/retention')
ret = json.loads(b) if s == 200 else {}
check('GET /retention 回報保留政策', s == 200 and ret.get('retention_days') == 30,
      f'{ret.get("retention_days")} 天 · 音檔 {ret.get("audio_bytes_now",0)/1048576:.1f} MB · '
      f'永久保留 {ret.get("kept_forever")}')

s, b = req('/sessions')
n = len(json.loads(b)['sessions']) if s == 200 else 0
check('GET /sessions 清單', s == 200 and n > 0, f'{n} 個 session')

print()
bad = [r for r in results if not r[1]]
print(f"總計 {len(results)} 項，失敗 {len(bad)} 項")
