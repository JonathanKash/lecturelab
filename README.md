# LectureLab

Turns real lecture recordings into notes, flashcards and quizzes you can study from instead of rewatching. It costs nothing beyond a Claude Max plan and Google AI Pro. v1 (the browser recorder) is at the `v1-archive` tag.

## What it does

1. **Pulls in recordings** from Canvas (Kaltura and Panopto included), Google Drive and video links, plus inbox folders you drop files into.
2. **Transcribes them** with Whisper large-v3 on a free Colab T4. It falls back to faster-whisper on your own CPU when Colab isn't available.
3. **Matches slides.** The deck comes from Canvas and each part of the transcript is matched to the slide on screen.
4. **Writes notes with Claude Code** on your Max plan.
   - Sonnet writes an outline, then each section from its slice of the transcript and its slides, then a summary, flashcards and a quiz.
   - Opus checks all of it against the transcript and removes or flags anything the lecture doesn't support.
5. **Exports Markdown** to `Documents\AI Notes\Lectures\<Course>\`: one notes file and one transcript per lecture. Anything that indexes that folder picks them up.

In the app you get the notes with timestamps that jump into the recording, Leitner flashcard review, a quiz, Anki export and full-text search across every lecture.

## Setup

You need Windows 11, Python 3.13, ffmpeg (`winget install ffmpeg`), LibreOffice for PowerPoint decks, and Claude Code signed in to a Max plan.

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\python -m playwright install chromium
.\.venv\Scripts\lecturelab install-tasks --start
```

That registers two scheduled tasks. "LectureLab Server" keeps the app running at http://127.0.0.1:8765 from logon, and "LectureLab Sync" runs every hour.

`.\.venv\Scripts\lecturelab shortcut` puts a LectureLab shortcut on your desktop. It opens the app in its own Chrome or Edge window, and starts the server first if it isn't running.

For Colab transcription, install `google-colab-cli` inside WSL Ubuntu and sign it in to your Google account once. Without it everything still works on your CPU, just slower.

For Canvas, click **Connect Canvas** in the app and sign in yourself. LectureLab keeps its own browser profile in `data/browser-profile/`. It never sees or stores your password, and it only reads from Canvas.

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

## Your data stays local

Recordings, transcripts, slides, the database, cookies and `.env` live in `data/` and are never committed.
