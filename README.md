# LectureLab

Turns real lecture recordings into notes, flashcards and quizzes you can study from instead of rewatching. It costs nothing beyond a Claude Max plan and Google AI Pro. v1 (the browser recorder) is at the `v1-archive` tag.

## What it does

1. **Pulls in recordings** from Canvas (Kaltura, Panopto and the course's Zoom cloud recordings included), Google Drive and video links, plus inbox folders you drop files into.
2. **Transcribes them** with Whisper large-v3 on a free Colab T4. It falls back to faster-whisper on your own CPU when Colab isn't available.
3. **Matches slides.** The deck comes from Canvas and each part of the transcript is matched to the slide on screen.
4. **Writes notes with Claude Code** on your Max plan.
   - Sonnet writes an outline, then each section from its slice of the transcript and its slides, then a summary, flashcards and a quiz.
   - Opus checks all of it against the transcript and removes or flags anything the lecture doesn't support.
5. **Exports Markdown** to `Documents\LectureLab Notes\<Course>\` (or a folder you pick): one notes file and one transcript per lecture. Anything that indexes that folder picks them up.

In the app you get the notes with timestamps that jump into the recording, Leitner flashcard review, a quiz, Anki export and full-text search across every lecture.

## Install on your laptop

You need a Windows 11 laptop and Claude Code signed in to your Claude plan. LectureLab was built and measured on a Max plan (notes for one lecture use about 68k output tokens, see below). Everything else is free, and setup installs it for you.

1. Go to the [latest release](https://github.com/JonathanKash/lecturelab/releases/latest) and, under Assets, download **Source code (zip)**.
2. Right-click the ZIP and choose **Extract All**. Setup won't run from inside the ZIP.
3. Open the extracted folder and double-click **setup.cmd**. If Windows warns that it came from the internet, choose Run (or More info, then Run anyway).
4. Answer its questions. It asks before installing Python 3.13 and ffmpeg with winget, and asks separately about LibreOffice, which you only need for PowerPoint slides. The first run takes 10 minutes or more, most of it downloading Python packages.
5. LectureLab opens on its Setup page. Add your courses, type your school's Canvas address if you use Canvas, and press "Check Claude's sign-in". The page says what each step still needs.

If Claude Code isn't installed yet, get it from [claude.com/claude-code](https://claude.com/claude-code) and run `claude` once in a terminal to sign in. Recordings still come in and get transcribed without it; notes wait until it works.

Sign Claude Code in with your Claude Pro or Max plan. If it's signed in to a Claude Console (API) account, or your plan has extra usage turned on, every lecture's notes are billed per use. LectureLab writes notes for each new lecture by itself, so that adds up.

After that, LectureLab starts by itself when you log in, and the desktop shortcut opens it.

**Where things live**

- The app: `%LOCALAPPDATA%\Programs\LectureLab`
- Your recordings, transcripts, settings and Canvas sign-in: its `data` folder
- Notes as Markdown: `Documents\LectureLab Notes` (you can pick another folder on the Setup page)
- Drop files here to add them: `Documents\LectureLab Inbox\<COURSE>\`

**Updating or repairing.** Download the newer release, extract it and run its setup.cmd. Your data, settings and notes are kept. Running setup.cmd again also repairs a broken install.

**Removing it.** In PowerShell:

```powershell
& "$env:LOCALAPPDATA\Programs\LectureLab\.venv\Scripts\lecturelab" stop
Unregister-ScheduledTask -TaskName 'LectureLab Server', 'LectureLab Sync' -Confirm:$false
```

Then delete the desktop shortcut and the `%LOCALAPPDATA%\Programs\LectureLab` folder. That folder holds your recordings and transcripts, so copy its `data` folder somewhere first if you want to keep them. Your notes folder isn't touched. The browser LectureLab uses for Canvas is in `%LOCALAPPDATA%\ms-playwright`; delete that too if nothing else of yours uses Playwright.

**If setup stops**, it says why and how to fix it, and the full log is in `%TEMP%\lecturelab-setup.log`. If it says LectureLab already runs from another folder, that's a copy you installed before. Answer yes to move to the new one, or no to leave the old one as it is.

Transcription runs on your laptop's CPU unless you set up Colab, which is much faster and free. See [Colab](#colab) below.

## Run it from a clone

For working on LectureLab itself. You need Windows 11, Python 3.13, ffmpeg (`winget install ffmpeg`), LibreOffice for PowerPoint decks, and Claude Code signed in to a Max plan.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\python -m playwright install chromium
.\.venv\Scripts\lecturelab install-tasks --start
```

That registers two scheduled tasks. "LectureLab Server" keeps the app running at http://127.0.0.1:8765 from logon, and "LectureLab Sync" runs every hour.

`.\.venv\Scripts\lecturelab shortcut` puts a LectureLab shortcut on your desktop. It opens the app in its own Chrome or Edge window, and starts the server first if it isn't running.

## Colab

Transcription is much faster on a free Colab GPU. It's optional: without it everything runs on your CPU, just slower. To set it up, install WSL Ubuntu (`wsl --install -d Ubuntu` in PowerShell run as administrator), install `google-colab-cli` inside it, and sign it in to your Google account once. The Transcripts step on the Setup page says which one LectureLab is using.

## Canvas

Type your school's Canvas address on the Setup page (it looks like `school.instructure.com`), then click **Connect Canvas** and sign in yourself. LectureLab keeps its own browser profile in `data/browser-profile/`. It never sees or stores your password, and it only reads from Canvas.

## Commands

```powershell
.\.venv\Scripts\lecturelab serve                # run the app in this window
.\.venv\Scripts\lecturelab sync                 # the hourly cycle, right now
.\.venv\Scripts\lecturelab notes 12             # notes for lecture 12 (no id: every lecture without notes)
.\.venv\Scripts\lecturelab notes 12 --offline   # no Claude at all, much plainer notes
.\.venv\Scripts\lecturelab stop
```

`sync` checks Canvas and the inbox folders, sends new audio to Colab in one batch, writes notes for anything transcribed and exports them. It's safe to run any time: a lock file stops two running at once, and a second run straight after does nothing.

## What it costs

Nothing extra. Measured on real 50-minute Programming Language Concepts lectures:

| Step | Time | Usage |
| --- | --- | --- |
| Transcription on the free Colab T4 | 43 min for 5 lectures (251 min of audio) | 0 Colab units |
| Notes for one 49-minute lecture | about 12 min (9 min Sonnet, 3 min Opus) | about 68k output tokens of plan usage |
| Regenerating notes that didn't change | a few seconds | none, every Claude call is cached by its input |

When Claude Code says the usage limit is reached, the job waits for the reset and carries on. It never falls back to the paid API: LectureLab removes `ANTHROPIC_API_KEY` from Claude Code's environment and refuses to run if the key is still visible.

## What leaves your laptop

Everything LectureLab keeps (recordings, transcripts, slides, notes, settings and your Canvas sign-in) stays on your laptop. It sends things out only to do its job:

- **Anthropic**, through Claude Code: each lecture's transcript and slide text, when notes are written.
- **Google Colab**, only if you set it up: the audio of each batch. The Colab session is shut down when the batch ends, and its disk goes with it. Without Colab, transcription runs on your CPU, and the first time it downloads the Whisper speech model (about 1.6 GB) from Hugging Face.
- **Canvas, Zoom, Panopto, Kaltura and Google Drive**: LectureLab reads your courses with your own sign-in and downloads recordings from where they're hosted. It never posts anything to Canvas, and it never sees your password.

There's no analytics and no update check. The app only listens on your own computer (127.0.0.1), so other devices on your Wi-Fi can't reach it, and other websites can't use it.

If you work on LectureLab itself: all of that lives in the gitignored `data/` folder and `.env`, so none of it is ever committed.
