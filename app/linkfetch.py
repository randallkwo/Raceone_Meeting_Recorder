#!/usr/bin/env python3
"""從「網路分享連結」取得音檔。

支援兩類來源：
  1. Google Drive 分享連結（用 gdown 處理確認頁／大檔 token）
  2. 其他 https 直連（串流下載，附瀏覽器 User-Agent）

安全設計（這是使用者貼進來的 URL，屬於不可信輸入）：
  - **只允許 https**，http 一律拒絕。
  - **主機白名單**：預設只放行已知的檔案分享服務（Drive／Dropbox／OneDrive），
    可用 `FETCH_ALLOW_HOSTS` 環境變數覆寫。這同時擋掉 SSRF ——
    否則 `http://127.0.0.1:8765/...` 或雲端 metadata 端點都會被摸到。
  - **拒絕私有位址**：白名單之外再檢查解析後的 IP 不落在私有／保留區段。
  - **大小上限**：邊下載邊累計，超過就中止並刪除半成品。
  - 不跟隨跨主機的重導（只跟同主機或白名單內主機）。
"""
import ipaddress
import os
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')

DRIVE_HOSTS = ('drive.google.com', 'docs.google.com',
               'drive.usercontent.google.com')

_DEFAULT_HOSTS = (
    'drive.google.com,docs.google.com,drive.usercontent.google.com,'
    'dropbox.com,www.dropbox.com,dl.dropboxusercontent.com,'
    '1drv.ms,onedrive.live.com'
)

ALLOW_HOSTS = tuple(
    h.strip().lower() for h in
    os.environ.get('FETCH_ALLOW_HOSTS', _DEFAULT_HOSTS).split(',') if h.strip()
)

CHUNK = 256 * 1024

# 由 Content-Type 推副檔名（拿不到就用 URL 猜，再不行給 m4a）
CT_EXT = {
    'audio/mp4': 'm4a', 'audio/x-m4a': 'm4a', 'audio/m4a': 'm4a',
    'audio/mpeg': 'mp3', 'audio/mp3': 'mp3',
    'audio/wav': 'wav', 'audio/x-wav': 'wav', 'audio/wave': 'wav',
    'audio/webm': 'webm', 'video/webm': 'webm',
    'audio/ogg': 'ogg', 'application/ogg': 'ogg',
    'audio/flac': 'flac', 'audio/x-flac': 'flac',
    'audio/aac': 'aac', 'audio/x-m4b': 'm4b',
}
KNOWN_EXT = {'m4a', 'mp3', 'wav', 'webm', 'ogg', 'flac', 'aac', 'mp4', 'm4b',
             'aiff', 'aif', 'opus', 'amr', '3gp', 'caf'}


class LinkFetchError(Exception):
    """使用者可讀的失敗原因。"""


def _is_private_host(host: str) -> bool:
    """解析 hostname，任一結果落在私有／保留網段就視為私有。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True                     # 解析不了 → 當成不安全
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return True
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return True
    return False


def validate(url: str) -> str:
    """檢查並正規化 URL。不合法就丟 LinkFetchError。"""
    url = (url or '').strip()
    if not url:
        raise LinkFetchError('沒有提供連結')
    if '://' not in url:
        url = 'https://' + url
    u = urllib.parse.urlparse(url)
    if u.scheme != 'https':
        raise LinkFetchError(f'只接受 https 連結（收到 {u.scheme or "無"}）')
    host = (u.hostname or '').lower()
    if not host:
        raise LinkFetchError('連結沒有主機名稱')
    if host not in ALLOW_HOSTS:
        raise LinkFetchError(
            f'不允許的主機：{host}。目前允許：{"、".join(ALLOW_HOSTS)}')
    if _is_private_host(host):
        raise LinkFetchError(f'主機解析到私有位址，拒絕：{host}')
    return url


def guess_name(url: str, content_type: str = '', content_disp: str = '') -> str:
    """決定存檔名稱（只回檔名，不含路徑）。"""
    name = ''
    if content_disp:
        # Content-Disposition: attachment; filename="x.m4a"
        import re as _re
        m = _re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", content_disp, _re.I)
        if m:
            name = urllib.parse.unquote(m.group(1).strip())
    if not name:
        p = urllib.parse.urlparse(url).path
        cand = Path(urllib.parse.unquote(p)).name
        if cand and Path(cand).suffix.lstrip('.').lower() in KNOWN_EXT:
            name = cand
    if not name:
        ext = CT_EXT.get((content_type or '').split(';')[0].strip().lower())
        name = f'link_audio.{ext}' if ext else 'link_audio.m4a'
    return name


def _fetch_http(url: str, dest: Path, max_bytes: int) -> tuple:
    """串流下載一般 https 直連。回傳 (檔名, 位元組, content-type)。"""
    req = urllib.request.Request(url, headers={'User-Agent': UA,
                                               'Accept': '*/*'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            final = r.geturl()
            fu = urllib.parse.urlparse(final)
            if (fu.hostname or '').lower() not in ALLOW_HOSTS:
                raise LinkFetchError(f'重導到不允許的主機：{fu.hostname}')
            ct = r.headers.get('Content-Type', '')
            name = guess_name(final, ct, r.headers.get('Content-Disposition', ''))
            declared = r.headers.get('Content-Length')
            if declared and int(declared) > max_bytes:
                raise LinkFetchError(
                    f'檔案過大（{int(declared)/1048576:.0f} MB），'
                    f'上限 {max_bytes//1048576} MB')
            written = 0
            with open(dest, 'wb') as f:
                while True:
                    buf = r.read(CHUNK)
                    if not buf:
                        break
                    written += len(buf)
                    if written > max_bytes:
                        raise LinkFetchError(
                            f'下載超過上限 {max_bytes//1048576} MB，已中止')
                    f.write(buf)
    except LinkFetchError:
        raise
    except Exception as e:
        raise LinkFetchError(f'下載失敗：{type(e).__name__}: {e}')
    return name, written, ct


def _fetch_drive(url: str, dest: Path, max_bytes: int) -> tuple:
    """用 gdown 取 Google Drive 檔案（處理確認頁與大檔 token）。"""
    try:
        import gdown
    except ImportError:
        raise LinkFetchError('伺服器缺少 gdown，無法處理 Google Drive 連結')

    # gdown 直接寫 dest；失敗會丟例外或回 None
    # 注意：gdown 6.x 已移除 `fuzzy` 參數，url 本身即支援各種 Drive 連結形式
    try:
        out = gdown.download(url, str(dest), quiet=True)
    except Exception as e:
        raise LinkFetchError(f'Google Drive 下載失敗：{type(e).__name__}: {e}')
    if not out or not dest.exists() or dest.stat().st_size == 0:
        raise LinkFetchError(
            'Google Drive 下載失敗（連結可能未公開分享，或需要授權）')
    size = dest.stat().st_size
    if size > max_bytes:
        dest.unlink(missing_ok=True)
        raise LinkFetchError(f'檔案過大（{size/1048576:.0f} MB），'
                             f'上限 {max_bytes//1048576} MB')
    # gdown 用網頁名稱時可能沒副檔名，補一個
    if not dest.suffix:
        dest.rename(dest.with_suffix('.m4a'))
    return dest.name, size, 'audio/*'


def fetch_to(url: str, dest: Path, max_bytes: int) -> tuple:
    """把 url 的檔案下載到 dest。回傳 (檔名, 位元組, content_type)。

    失敗一律丟 LinkFetchError，且不留半成品。
    """
    url = validate(url)
    host = (urllib.parse.urlparse(url).hostname or '').lower()
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if host in DRIVE_HOSTS:
            name, size, ct = _fetch_drive(url, dest, max_bytes)
        else:
            name, size, ct = _fetch_http(url, dest, max_bytes)
        if size == 0:
            raise LinkFetchError('下載到 0 位元組的檔案')
        return name, size, ct
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    # 手動測試：python3 linkfetch.py <url> <dest>
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    try:
        n, s, c = fetch_to(sys.argv[1], Path(sys.argv[2]),
                           int(os.environ.get('MAX_UPLOAD_BYTES', 200 * 1024 * 1024)))
        print(f'OK {n} {s} bytes {c}')
    except LinkFetchError as e:
        print(f'FAIL {e}')
        sys.exit(1)
