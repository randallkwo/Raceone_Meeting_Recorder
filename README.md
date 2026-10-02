# RaceOne Meeting Recorder

A self-hosted meeting recorder: record from your phone, get a bilingual
transcript and minutes back, with the audio never leaving machines you
control.

Born from a practical need — long, technical meetings that had to be
transcribed and summarised without shipping confidential audio to a
third-party API.

![status](https://img.shields.io/badge/status-running-brightgreen)
![license](https://img.shields.io/badge/license-MIT-blue)

---

## What it does

1. **Record** in the browser (installable PWA) or upload an existing file.
2. **Paste a share link** instead, if the file is already in cloud storage —
   the server pulls it, so you can fetch recordings larger than any upload
   limit your proxy imposes.
3. **Transcribe** on the server (local CPU by default, GPU optional).
4. **Summarise** into bilingual minutes — a Chinese and an English section —
   via an LLM.
5. **Archive** to cloud storage and notify you.

The transcript is written from the audio only. When the audio contains no
real discussion, the minutes say so rather than inventing content — see
[Design decisions](#design-decisions).

---

## Architecture

```
iPhone / desktop browser (PWA)
        │  5-minute slices, or one whole file, or a share link
        ▼
VPS  ── HTTP API (:8765) ─────────────────────────────────────┐
        │                                                     │
        ├── session queue (one worker at a time)              │
        │        │                                            │
        │        ▼                                            │
        │   ffmpeg → 16 kHz mono WAV                          │
        │        │                                            │
        │        ├── faster-whisper (CPU, default)            │
        │        └── Runpod serverless (GPU, optional)  ──────┤
        │        │                                            │
        │        ▼                                            │
        │   LLM → 中文紀要 + English minutes + speaker notes  │
        │        │                                            │
        │        ▼                                            │
        └── cloud storage archive + chat notification ─────────┘
```

The worker runs in its own cgroup so that a memory-hungry transcription
cannot take the web process down with it — see
[Design decisions](#design-decisions).

---

## Design decisions

The parts that were non-obvious, and why they are the way they are.

### Recording in 5-minute slices

iOS Safari will not let a page record indefinitely in the foreground, and a
single long upload has no recovery path. Slicing bounds the damage from a
dropped connection and makes the session resumable.

Merging the slices turned out to be the hard part: concatenating encoded
audio with `-c copy` produces a **0-byte file** when the slices are Opus in
WebM. The merge re-encodes through a filter graph instead, and treats a
zero-byte result as a failure rather than passing it downstream.

### Minutes that refuse to invent

An early version produced confident, plausible minutes from an almost-empty
transcript. Minutes are only useful if they can be trusted, so the prompt
carries an explicit instruction to report "no substantive discussion
captured" when that is what the transcript shows. A summary that
occasionally says "nothing here" beats one that is occasionally fiction.

The model's own heading is also stripped — it likes to prepend a title, and
a title inside a titled document is noise.

### Pulling from a share link, server-side

Uploads pass through a reverse proxy, which caps body size. Rather than
fight the cap, `POST /fetch-link` accepts a cloud share URL and downloads on
the server. The URL is **untrusted input**, so it is validated before the
request is accepted:

- `https` only
- host must be on an allow-list
- the same checks are re-applied to every redirect hop
- size is accumulated during the download and aborted past a limit
- a rejected URL returns `400` immediately — never a `202` that fails later

### GPU without making the audio public

Local CPU transcription is fine until a meeting runs long: measured RTF
**0.12x** on a 2-vCPU box, so an 8-hour recording takes about 52 minutes.

A GPU endpoint measured **0.031x** — but the usual way to feed one a large
file is to hand it a public URL, and meeting audio cannot be public.

So `gpu_transcribe.py` **chunks instead**:

- split into 10-minute mp3 slices, submitted in parallel
- each slice carries **15 s of overlap** on both sides
- merging keeps only segments whose midpoint falls inside that slice's
  *owned* range

A word on a boundary therefore lands inside at least one slice's owned
range: nothing is dropped, nothing is duplicated. Verified on a real
22.6-minute meeting:

| Check | Result |
|---|---|
| Characters, GPU vs local | 11 152 vs 11 105 (0.4 % — nothing lost) |
| Gaps over 30 s | none |
| Ending | 1353.0 s of a 1353.5 s recording |
| Duplicated segments at boundaries | 0 |

All slices are submitted **at once** rather than as workers free up. With a
5-second idle timeout, letting the queue drain scales a worker down and
pays the ~55 s cold start again for the next slice.

### One worker, in its own cgroup

whisper-small peaks near 900 MB, and two threads push that to ~1 250 MB. On
a small host that is enough to trigger a global out-of-memory kill — which
took down the web process, not the transcription.

The worker is therefore started in a dedicated systemd scope with a memory
cap and a high `oom_score_adj`, so that under pressure the kernel reaps the
transcription and leaves everything else alone. The cap is set from measured
peak usage, not a guess; raising the thread count without raising the cap
would simply get the worker killed mid-job.

### Reading is authenticated too

The write endpoints were token-protected from the start; the read endpoints
were not, which meant anyone who could reach the host could list sessions
and read every transcript. All data endpoints now require the token
(`X-Meeting-Token` header, `?token=`, or an HttpOnly cookie), while `/` and
`/health` stay public so the PWA and monitoring keep working.

### Long recordings are transcribed in windows, not in one pass

faster-whisper decodes the entire recording into a float32 array before it
starts, so peak memory scales with duration and ignores bitrate:

| Audio length | Decoded array |
|---|---|
| 3 minutes | ~11 MB |
| 3.1 hours | ~713 MB |

A 3.1-hour recording added to the model's ~500 MB reached 1 834 MB and was
killed against the 1 800 MB cap derived from a 3-minute benchmark. The cap
was right; the assumption behind it was not.

Recordings are now transcribed through fixed windows (default 1800s). Each
window carries 15s of overlap on both sides, and merging keeps only the
segments whose midpoint falls inside that window's owned range, so a word
on a boundary lands in exactly one window. Peak memory then depends on one
window rather than the total.

Window size matters: against a single-pass run on a 22.6-minute meeting,
600s windows differ by 0.2% in characters while 300s windows differ by
12.7%. Cutting too finely costs accuracy, so the default is generous.

### Long recordings go to the GPU without being asked

Measured on the same host: CPU RTF 0.144x, GPU 0.0175x. For a 3.1-hour
recording that is ~27 minutes versus ~3.2 minutes, at a cost of about
$0.06, and it leaves the two vCPUs free instead of saturating them.

The break-even point — including the serverless cold start — is around
7 minutes of audio, so the default threshold is a conservative 1200s.

The important half is the fallback. Any GPU problem (missing key, endpoint
down, exhausted balance, chunk failure) falls back to local CPU
automatically. The GPU is an optimisation; it must never be the reason a
user does not get a transcript.

### Local audio is dropped once the archive exists

Audio is deleted as soon as it has been transcribed *and* the copy on
Drive is confirmed. Until that confirmation, the local file is the only
copy — so a failed archive always keeps it. Transcripts, minutes and
metadata are never deleted; they are small and they are the actual
deliverable.

One consequence worth knowing: "re-transcribe" needs the local audio, so
after a purge it is unavailable and the endpoint says so explicitly rather
than failing vaguely.

---

## Measured performance

Real meeting audio, identical parameters, transcription only:

| Engine | Model | RTF | 8-hour meeting |
|---|---|---|---|
| CPU, 1 thread | small | 0.168x | ~81 min |
| **CPU, 2 threads** | small | **0.108–0.144x** | **~52–69 min** |
| CPU, 4 threads | small | 0.117x | ~56 min |
| **Runpod GPU** | **large-v2** | **0.0175–0.022x** | **~8–11 min** |

Four threads is *slower* than two on a 2-vCPU host — oversubscription costs
more than it buys.

Measured end to end on a 3.1-hour recording: 19 windows, 407 segments,
194.7s wall clock at RTF 0.0175x, approximately $0.06.

Downloading, by contrast, is not worth optimising: cloud storage bursts for
the first ~64 MB and then throttles hard, so the ceiling is the provider's
rather than the client's.

---

## Setup

### Requirements

- Linux host with Python 3.11+
- `ffmpeg`, `rclone` (for archiving)
- Optional: a Runpod account for the GPU path

### Install

```bash
sudo mkdir -p /opt/meeting-recorder/app /var/lib/meeting-recorder
sudo chown "$USER" /opt/meeting-recorder/app /var/lib/meeting-recorder
git clone <this repo> /opt/meeting-recorder/app
cd /opt/meeting-recorder/app
python3 -m venv venv
./venv/bin/pip install faster-whisper requests gdown
```

### Configure

Copy `config/meeting-recorder.env.example` to
`/etc/meeting-recorder/meeting-recorder.env`, `chmod 600`, and fill it in.

| Variable | Purpose | Default |
|---|---|---|
| `MEETING_TOKEN` | Shared secret for every non-public endpoint | — (auth off; localhost only) |
| `MEETING_DATA_DIR` | Where sessions live | `/var/lib/meeting-recorder` |
| `MEETING_ENV_FILES` | Env files to load, colon-separated | `/etc/meeting-recorder/meeting-recorder.env` |
| `MEETING_PORT` | Listen port | `8765` |
| `MEETING_CHAT_ID` | Chat to notify on completion | — (no notification) |
| `DRIVE_REMOTE` | rclone remote for archiving | `gdrive:Meetings` |
| `WHISPER_MODEL` | Model size | `small` |
| `WHISPER_THREADS` | Transcription threads | `2` |
| `RUNPOD_ENDPOINT_ID` | Enables the GPU path | — (CPU only) |

No secret has a default value. A missing notification target means no
notification, not a notification to somebody else's chat.

### Run

```bash
sudo cp config/meeting-recorder.service config/meeting-recorder-cleanup.* /etc/systemd/system/
sudo systemctl enable --now meeting-recorder
```

`meeting-recorder-cleanup.timer` prunes local audio on a retention schedule
and retries any archiving that failed.

### GPU path (optional)

```bash
RUNPOD_ENDPOINT_ID=<id> ./venv/bin/python gpu_transcribe.py <session-id>
```

Output is identical in shape to the CPU path, so nothing downstream changes.

---

## Security notes

- Credentials live only in a `600` env file outside the repository.
- Meeting audio is not sent to any third-party API by default; the GPU path
  sends chunks to your own endpoint over TLS rather than publishing a URL.
- `POST /fetch-link` treats its URL as hostile input and validates scheme,
  host, redirects and size.
- Failed or rejected input returns an error status — never a success that
  fails after the fact.

If you deploy this, change `MEETING_TOKEN`, and put the service behind TLS.

---

## Layout

```
app/
  server.py         HTTP API + PWA host, session queue, auth
  transcribe.py     worker: ffmpeg, whisper, minutes, archive, retention
  gpu_transcribe.py optional GPU path (chunked, parallel)
  linkfetch.py      share-link downloader with host allow-list
  index.html        the PWA
  verify_e2e.py     end-to-end checks against a running instance
config/             systemd units and the env template
docs/design.md      the original design document
```

---

## Engineering notes

Kept because they cost real time to find.

- **A tool's exit code can lie.** A cleanup script printed
  `failed: 3` and still exited `0`, so the retry loop around it decided the
  job was done and stopped. Report failure in the exit status, not only in
  the output.
- **`grep … | head` always succeeds.** The exit status of a pipeline is the
  last command's, and `head` returns `0` on empty input — so a secret scan
  built that way flags everything and verifies nothing. Test `grep`'s own
  status, or scan in a language that does not have this trap.
- **Verify the instrument, not just the number.** Process `RSS` summed
  across children double-counts shared pages; `du` on package directories
  reports unpacked size and counts the same content twice. A measurement
  without its method attached is not a measurement.
- **Check what the tool systematically misses** before quoting it. One
  download benchmark looked like a 20x win until a second, larger run
  showed the provider throttling after the first 64 MB.
- **Delete only after confirming nothing depends on it.** `grep -rl` for
  references and check the dependency chain; an unreviewed removal can break
  automation that is not visible from where you are standing.

### 2026-10-02 Changelog

- **PWA nav fix**: `target="_blank"` links (minutes / speakers / JSON)
  opened in an embedded webview with no back button — users were stuck.
  All replaced with in-app buttons, zero `<a>` links in the results pane.
- **Mobile text wrapping**: `white-space: pre-wrap` breaks only at whitespace.
  Added `word-break: break-word; overflow-wrap: anywhere` in three places.
  Measured: 200-char no-space string overflowed 3× before, 0× after.
- **iPhone home indicator**: bottom toolbar now has `safe-area-inset-bottom`.
- **TypeError guard**: `setToolbar('tSpeakers')` no longer throws when the
  view has no speaker button.
- **server CSS typo**: `flex-direction_column` → `flex-direction: column`.

---

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it; just keep the
copyright notice.
