# LectureLab v2

Turns real lecture recordings into notes, flashcards and quizzes good enough to skip rewatching. v1 (browser recorder) is archived at tag `v1-archive`; don't reuse its code unless it's clearly the best option.

## Hard rules
- **Free means free.** No paid APIs anywhere. The only costs are the Claude Max plan and Google AI Pro.
- **Notes come from Claude Code headless on the Max plan**: `claude -p ... --output-format json`, never the Anthropic API or SDK.
  - Every subprocess call to `claude` goes through `claude_call()` in `lecturelab.py`. It copies `os.environ`, pops `ANTHROPIC_API_KEY`, and refuses to run if the key is still visible to the child.
  - Never pass `--bare`: it forces API-key auth.
  - `--model sonnet` for outline, sections, flashcards and quiz. `--model opus` only for the final check pass.
  - Cache every call by input hash. On "usage limit reached", park the job and retry later. The offline generator runs only when the user asks.
- **Transcription runs on a Colab GPU** through the official Colab CLI (`google-colab-cli`) inside WSL Ubuntu.
  - Call it with `wsl.exe -d Ubuntu -e ...`. The default distro is docker-desktop, so always name Ubuntu.
  - One Colab session per batch, and `colab stop` always runs in a `finally` block.
  - Below 20 compute units, or after 2 consecutive Colab failures, use local CPU faster-whisper.
- **Never commit:** recordings or any media, transcripts of classes, slides, cookies, tokens, `.env`, the database, `server.log`. All of these live in gitignored `data/` or `.env`. Check `git diff --cached --name-only` before every commit.
- **Canvas has no API token** (UF doesn't give students one). LectureLab has its own Playwright browser profile in `data/browser-profile/`. The user signs in there themselves with GatorLink and Duo via "Connect Canvas".
  - Never ask for, fill in or store the UF password, and never retry a login automatically.
  - Canvas is read with that session, GET `/api/v1/...` only (`CanvasSession.get` refuses anything else).
  - Kaltura, Panopto and other videos behind UF login get a Netscape cookie file exported from that profile and passed to yt-dlp as `--cookies`. Chrome's own cookies can't be read on Windows.
  - An expired session means: mark it "needs sign-in", notify once, keep everything else running. Inbox folders always work without Canvas.
- **Colab:** at 0 units the free-tier T4 is used. With a small paid balance (0 < units < 20) the batch goes to the local CPU.
  - `colab upload` can't create folders and rejects big files, so batches `mkdir` first and upload audio in checksummed 8 MB parts.

## Environment
- Windows 11, PowerShell, VS Code. No NVIDIA GPU. Python 3.13 in `.venv` (the system default is 3.14; don't use it here).
- ffmpeg and LibreOffice were installed with winget. yt-dlp is in the venv; upgrade it with `pip install -U yt-dlp` when a site breaks.
- Playwright is a runtime dependency (the Canvas browser). After a fresh install, run `.venv\Scripts\python -m playwright install chromium` once.
- The v2 server is on **127.0.0.1:8765**. Port 8000 may still be v1.

## Layout
- `lecturelab.py`: all server-side code (config, DB, ingest, transcription, slides, notes, export, sync, FastAPI).
- `cli.py`: the `lecturelab` command (`serve`, `supervise`, `stop`, `install-tasks`, and more as stages land).
- `colab/transcribe.py`: runs inside the Colab VM.
- `static/index.html`: the whole UI. Vanilla JS, with KaTeX from a CDN as the only library. No Node build step.
- `tests/test_unit.py`, `tests/test_e2e.py`: pytest. The e2e suite starts a supervised server on port 8766 with a temp data dir and drives the installed Chrome with Playwright.
- Code stays consolidated in these files. Split something out only when it's genuinely separate.

## Running
```powershell
.\.venv\Scripts\lecturelab serve              # foreground
.\.venv\Scripts\lecturelab install-tasks --start   # logon task: hidden, auto-restart, logs to server.log
.\.venv\Scripts\python -m pytest              # unit + e2e
```

## Working rules
- Test every stage end to end in a real browser with Playwright against the running server, using a real 5 to 10 minute lecture clip (gitignored `data/fixtures/`), not a synthetic tone.
- Commit after each working stage. Short imperative subject, one logical change per commit.
