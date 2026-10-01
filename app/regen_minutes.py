#!/usr/bin/env python3
"""只重生某場會議的雙語紀要 —— 不重跑 whisper。

既有 `sessions/transcripts/<sid>.json` 已經包含 segments 時，重跑整場轉錄
（60 分鐘音檔約 15 分鐘）是浪費。本工具直接讀 segments 重產 `minutes/<sid>.md`。

用法：
  cd /opt/meeting-recorder/app
  venv/bin/python regen_minutes.py <sid> [<sid> ...]

注意：直接 `import transcribe` **不會**載入環境變數（`load_env()` 只在
`__main__` 呼叫），所以本腳本必須自己先呼叫 `load_env()`，否則
`OPENROUTER_API_KEY` 讀不到，摘要會靜默回傳空字串。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcribe as T   # noqa: E402


def build_md(sid: str, zh: str, en: str, attribution: str = '') -> str:
    info = T.read_meta(sid)
    roster = [s.strip() for s in (info.get('speakers') or []) if s and s.strip()]

    blk = [f'# 會議紀要 {sid}', '']
    if info.get('filename'):
        blk.append(f'來源音檔：{info["filename"]}')
    if roster:
        blk.append(f'與會者名單：{"、".join(roster)}')
    blk.append('')
    if zh:
        blk.append('## 中文紀要')
        blk.append(T.clean_minutes(zh))
    if en:
        blk += ['', '## English Minutes', '', T.clean_minutes(en)]
    if attribution:
        blk += ['', '## 發言歸屬（文字推理，非聲紋辨識）', '', attribution]
    return '\n'.join(blk) + '\n'


def regen(sid: str) -> bool:
    j = T.TRANS_DIR / f'{sid}.json'
    if not j.exists():
        print(f'{sid}: 找不到 {j.name}，先跑轉錄再來', file=sys.stderr)
        return False

    import json
    data = json.loads(j.read_text(encoding='utf-8'))
    segments, meta = data.get('segments') or [], data.get('meta') or {}
    if not segments:
        print(f'{sid}: segments 為空，無法產生紀要', file=sys.stderr)
        return False

    lang = T.detect_lang(meta)
    zh, en = T.summarize_bilingual(segments, lang)
    if not (zh or en):
        print(f'{sid}: OpenRouter 沒有回應（檢查 OPENROUTER_API_KEY）', file=sys.stderr)
        return False

    T.MINUTES_DIR.mkdir(parents=True, exist_ok=True)
    md = T.MINUTES_DIR / f'{sid}.md'
    md.write_text(build_md(sid, zh, en), encoding='utf-8')
    print(f'{sid}: 主語言={lang} zh={len(zh)}字 en={len(en)}字 → {md} '
          f'({md.stat().st_size} bytes)')
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description='重生雙語會議紀要（不重跑 whisper）')
    ap.add_argument('sessions', nargs='+', help='session id（可多個）')
    a = ap.parse_args()

    T.load_env()   # 必須：載入 OPENROUTER_API_KEY
    if not T.os.environ.get('OPENROUTER_API_KEY'):
        print('警告：OPENROUTER_API_KEY 未設定，摘要會失敗', file=sys.stderr)

    ok = sum(regen(s) for s in a.sessions)
    print(f'完成 {ok}/{len(a.sessions)}')
    return 0 if ok == len(a.sessions) else 1


if __name__ == '__main__':
    sys.exit(main())
