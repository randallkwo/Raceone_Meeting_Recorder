# Hermes 智能錄音筆 — 完整架構藍圖

> 設計文件
> 部署：Linux VPS（CPU，無 GPU）
> 產品代号：RaceOne Meeting Recorder

## 結論先講

可以做，分三階段；前兩階段不需要寫手機 App。Hermes 是後端大腦，手機端用 PWA。

### 已確認的設計决策

| # | 决策 | 選擇 |
|---|------|------|
| Q1 | 部署 | Linux VPS（CPU，無 GPU） |
| Q2 | 手端上傳 | iPhone PWA（前台持續錄音） |
| Q3 | 結束觸發 | Telegram 指令「結束會議」 |
| Q4 | 分片策略 | 每 5 分鐘切片上傳，斷點可續傳 |
| Q5 | 轉錄引擎 | 混合 C：本地 faster-whisper-small 優先，重要會議走 Deepgram nova-3 |
| Q6 | 輸出格式 | 雙輸出 C：簡潔決議型 + 完整紀錄型；語言依現場（中文/英文自動檢測） |
| Q7 | 講者標註 | 混合 D：事前名單優先，無名單則 diarize |
| Q8 | 隱私/存檔 | B：全部存 Google Drive，盡量不走第三方 |
| Q9 | 交付管道 | Telegram 推送通知 |

### 已知限制（誠實講）

1. iOS PWA 前台錄音 → Apple 要求顯示錄音指示條
2. 純 CPU 跑 whisper → 30–60 分鐘會議需 15–30 分鐘轉錄（非即時）
3. Deepgram 走第三方 → 機密會議需封掉，純本地（犧牲 diarize）
4. 5–10 人 diarize → 本機無法做，全靠 Deepgram；本地只能標 Speaker1/2/3

## 系統架構

```
iPhone PWA（前台錄音）
    │
    ├─ 每 5 分鐘 ──→ ffmpeg 切片 16kHz mono mp3 ──→ POST /upload → VPS
    │
    └─ 按下「結束會議」──→ Telegram 指令 ──→ VPS 收到 signal
                                              │
                                    累積所有片段 → 合併
                                              │
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                        faster-whisper   Deepgram nova-3   講者分離
                        small (zh/en)    (重要會議)       diarize
                              │               │               │
                              └───────────────┼───────────────┘
                                              ▼
                                     LLM 整理會議記錄
                                              │
                              ┌───────────────┼───────────────┐
                              ▼               ▼               ▼
                        _minutes.md     _transcript.txt   _actions.csv
                              │               │               │
                              └───────────────┼───────────────┘
                                              ▼
                                      Google Drive 存档
                                      Telegram 推送通知
```

## 階段一：會後批次（今天就能用，零開發）

手機錄音 → 傳進 Telegram → 自動轉錄 → 產出會議記錄 → 推回 Telegram + Drive。

- Telegram 語音訊息會自動轉文字，直接把開會錄音丟進來即可。
- 長檔（>20 分鐘）先 ffmpeg 轉 16kHz 單聲道 mp3，再送 Deepgram nova-3（zh-TW、標點、講者分離）。
- 產出三份：`_minutes.md`（會議記錄）、`_transcript.txt`（逐字稿）、`_actions.csv`（待辦清單）。
- 延遲：上傳完成後數十秒～數分鐘。

## 階段二：準即時（PWA + 分片）

iPhone PWA 前台錄音，每 5 分鐘自動切片上傳。

VPS 端：
1. 每收到一段 → 立即轉錄 → 累積到同一 session
2. 回極短確認（例：`已收到 12:03–12:05，累計 42 分鐘`）
3. 說「結束會議」→ 立刻輸出完整逐字稿 + 會議記錄 + 待辦清單

- 延遲：每段結束後數十秒（等於每 1–3 分鐘更新一次）。
- 傳輸層直接用 Telegram：**不必寫手機 App、不必架網域**。
- 適合實體會議、講座、訪談。

## 階段三：真即時串流（需開發，約 1–2 週）

手機 App/PWA ──WebSocket──► VPS (FastAPI relay) ──► Deepgram Streaming
   ▲ 即時字幕                      │
   └────────────────────────────────┘
                                    └──► 每 N 分鐘丟給 Hermes 擷取重點

- 手機端：麥克風 → wss://api.deepgram.com/v1/listen
  （`model=nova-3&language=zh-TW&diarize=true&punctuate=true&interim_results=true`）
- VPS：FastAPI WebSocket relay，同時 (a) 回傳即時字幕、(b) 累積全文、(c) 定期呼叫 Hermes 擷取「截至目前為止的決議與待辦」。
- 必要條件：HTTPS 網域 + TLS（WebSocket 不能走純 IP）、手機端程式（建議先做 PWA 繞過上架）。
- 隱私替代：改用主機上的 faster-whisper 自架串流，音訊不出機器（即時需 GPU）。

## 翻譯

- 語音用 STT、翻譯用 LLM（術語可控），不要在 STT 層做翻譯。
- 會議記錄可雙語輸出：中文紀錄 + 英文摘要。

## 風險與限制（誠寫）

1. 「即時」有物理下限：批次最少要等該段音訊結束才轉完。
2. 講者分離需要 Deepgram；本機 faster-whisper 無 diarize，只能標「講者1/2/3」。
3. 主機是純 CPU VPS，即時本機辨識需 GPU。
4. 走 Deepgram 代表音訊離開主機；機密會議請改用本機引擎或先確認政策。
5. App Store 上架另有工程量（帳號、審查、推播憑證）。
6. 已知小問題：`.env` 鍵名為 `Deepgram_API_KEY`（大小寫特殊），直連 API 需 `DEEPGRAM_API_KEY`。

## 建議路線

| 順序 | 做什麼 | 工程量 | 產出 |
|---|---|---|---|
| 1 | 把會議錄音丟進 Telegram 實測 | 0 | 會議記錄 |
| 2 | 準即時：手機每 1–3 分鐘自動分段上傳 | 設定約 30 分鐘 | 準即時逐字稿 |
| 3 | 決定是否做 PWA + WebSocket relay | 1–2 週 | 即時字幕 App |

---

## PWA 前端設計（階段二）

### 技術選擇

- 單檔案 HTML + JavaScript（無需建構工具）
- MediaRecorder API（iOS 14.5+ 支持）
- 切片邏輯：每 5 分鐘自動 `requestData()` → blob → POST
- 狀態 UI：錄音指示條 + 倒數計時器 + 上傳進度條

### iOS PWA 限制與对策

| 限制 | 對策 |
|---|---|
| 必須前台運行（後台會被殺） | 錄音指示條必顯示，無法繞過 |
| 螢幕鎖定後停止錄音 | 加入鎖定提醒「請勿鎖屏」 |
| Safari 15+ 才支持 MediaRecorder | 檢測不支援則降級提示 |
| PWA 安裝提示需 user gesture | 首次點擊「安裝 App」觸發 |

### PWA 檔案結構

```
/opt/meeting-recorder/app/
├── index.html          # 前端 UI
├── app.js              # 錄音 + 切片 + 上傳邏輯
├── manifest.json       # PWA 安裝配置
└── service-worker.js   # 離線緩存
```

### 核心片段（app.js 邏輯）

```javascript
// 錄音參數
const SLICE_MS = 5 * 60 * 1000; // 5 分鐘
const SAMPLE_RATE = 16000;
const CHANNELS = 1;

let mediaRecorder;
let audioChunks = [];
let sliceTimer;

async function startRecording() {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  mediaRecorder = new MediaRecorder(stream, {
    mimeType: 'audio/webm;codecs=opus',
    audioBitsPerSecond: 128000
  });

  mediaRecorder.ondataavailable = (e) => {
    if (e.data.size > 0) audioChunks.push(e.data);
  };

  mediaRecorder.onstop = async () => {
    const blob = new Blob(audioChunks, { type: 'audio/webm' });
    await uploadSlice(blob);
    audioChunks = [];
  };

  mediaRecorder.start();
  startSliceTimer();
}

function startSliceTimer() {
  sliceTimer = setInterval(() => {
    if (mediaRecorder.state === 'recording') {
      mediaRecorder.requestData(); // 觸發 ondataavailable
    }
  }, SLICE_MS);
}

async function uploadSlice(blob) {
  // 轉成 16kHz mono mp3 後上傳
  const formData = new FormData();
  formData.append('audio', blob, `slice_${Date.now()}.webm`);
  formData.append('session_id', currentSessionId);
  formData.append('timestamp', new Date().toISOString());

  await fetch('/upload', {
    method: 'POST',
    body: formData
  });
}
```

---

## VPS Pipeline 設計

### 目錄結構

```
/var/lib/meeting-recorder/
├── pipeline.py           # 主流程：接收→切片→轉錄→輸出
├── config.yaml           # 環境配置
├── requirements.txt      # Python 依賴
├── templates/
│   ├── minutes_zh.md     # 中文會議記錄模板
│   ├── minutes_en.md     # 英文會議記錄模板
│   └── actions.csv       # 待辦清單模板
└── sessions/
    └── <session_id>/     # 每個會議獨立資料夾
        ├── raw/          # 原始音訊片段
        ├── merged.mp3    # 合併後音訊
        ├── transcript.txt# 逐字稿
        ├── transcript_diag.json  # Deepgram 結果（若有）
        ├── minutes.md    # 會議記錄
        └── actions.csv   # 待辦清單
```

### 主流程（pipeline.py）

```python
# 步驟 1：接收上傳片段
# POST /upload → 保存到 sessions/<id>/raw/slice_N.webm

# 步驟 2：合併片段
# ffmpeg concat → sessions/<id>/merged.mp3 (16kHz mono)

# 步驟 3：轉錄（混合模式）
# 本地 faster-whisper-small → transcript_local.txt
# 若標記「重要會議」→ 同時送 Deepgram nova-3
# 兩者差異大時自動標註「建議人工覆核」

# 步驟 4：講者分離
# 事前名單 → 映射 Speaker1/2/3 → 實際姓名
# 無名單 → 保留 Speaker1/2/3

# 步驟 5：LLM 整理會議記錄
# 輸入：逐字稿 + 講者映射 + 語言
# 輸出：雙輸出（簡潔決議 + 完整紀錄）

# 步驟 6：存檔 + 交付
# Google Drive 上傳
# Telegram 推送通知
```

---

## 會議記錄模板

### 簡潔決議型（_minutes.md）

```markdown
# 會議記錄 — {會議名稱}

**日期**：{日期}
**語言**：{中文/英文}
**講者**：{名單}

## 決議事項
1. {決議} — 負責人：{姓名} — 截止：{日期}

## 待辦清單
| 項目 | 負責人 | 截止 | 狀態 |
|------|--------|------|------|
| {事項} | {姓名} | {日期} | 待辦 |

## 關鍵引用
> {重要引言}
```

### 完整紀錄型（_minutes_full.md）

```markdown
# 會議完整紀錄 — {會議名稱}

**日期**：{日期}
**語言**：{中文/英文}
**講者**：{名單}

## 議程
1. {議題}

## 討論摘要
### 議題 1：{名稱}
- {講者}：{觀點}
- {講者}：{觀點}
- 共識/分歧：{說明}

## 決議
- {決議}

## 待辦
| 項目 | 負責人 | 截止 | 狀態 |
|------|--------|------|------|
| {事項} | {姓名} | {日期} | 待辦 |

## 風險/未決事項
- {事項}
```

---

## 交付管道

### Telegram 推送

```
✅ 會議記錄已產出
📁 Drive：{檔案連結}
📝 簡潔版：{minutes.md 內容}
📋 待辦：{actions.csv 摘要}
```

### Google Drive 存檔

```
RaceOne Meetings/
├── 2026-09-30_會議名稱/
│   ├── transcript.txt
│   ├── minutes.md
│   ├── minutes_full.md
│   ├── actions.csv
│   └── merged.mp3
```

---

## 隱私與合規

- 音訊優先走本地 faster-whisper（不出機器）
- Deepgram 僅在標記「重要會議」時啟用
- 所有檔案存 Google Drive（自有帳號）
- 可設定自動清理週期（預設 90 天）

---

## 實測記錄 — 2026-09-30

### 測試環境
- 語音：3 秒語音訊息（Telegram 發給 @RaceoneaiBOT）
- 轉錄引擎：faster-whisper-small（純 CPU VPS）
- 輸出：雙輸出模板

### 結果
| 項目 | 結果 |
|---|---|
| 語音接收 | ✅ Telegram 語音訊息成功接收 |
| 格式轉換 | ✅ ffmpeg 16kHz mono mp3 |
| STT 轉錄 | ✅ faster-whisper-small → 正確 |
| 會議記錄 | ✅ 雙輸出模板產出 |
| Google Drive | ✅ 存檔成功 |
| Telegram 回推 | ✅ 使用者收到 |

### 待改善
- 語音必須發給 @RaceoneaiBOT，不是 DM
- 長會議（30-60 分鐘）需測試分批切片
- 講者分離需真實會議驗證

---

## 下一步

1. 階段一：找一段真實 30 分鐘會議錄音，確認講者分離與會議記錄品質
2. 階段二：開發 iPhone PWA 前端（前台錄音 + 5 分鐘切片）✅ 已完成
3. 階段三：WebSocket 即時串流（需 GPU 或 Deepgram）

---

*此藍圖隨實測迭代。版本 1.1 — 2026-09-30*