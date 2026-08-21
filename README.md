# LectureLab

Local lecture-capture and study-notes app. Record your professor through the microphone,
transcribe **locally** with [faster-whisper](https://github.com/SYSTRAN/faster-whisper),
and turn transcripts into organized study notes, flashcards, and quizzes with the
Anthropic API — with a fully offline, no-AI fallback so everything still works without a key.

- **Backend:** FastAPI + SQLite (`lecturelab.db`, created automatically)
- **Frontend:** single-page vanilla JS, no build step, KaTeX for formulas
- **Transcription:** faster-whisper on CPU, fully local
- **AI notes:** Anthropic API (`claude-sonnet-4-6`) — optional

## Setup

Requires **Python 3.11–3.13** (3.13 recommended; faster-whisper's native wheels may
not be available yet on 3.14).

```powershell
# 1. Create and activate a virtual environment
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1        # (macOS/Linux: source .venv/bin/activate)

# 2. Install dependencies
pip install -r requirements.txt

# 3. (Optional) enable AI note generation
$env:ANTHROPIC_API_KEY = "sk-ant-..."   # (macOS/Linux: export ANTHROPIC_API_KEY=...)

# 4. Run
python app.py
```

Open **http://localhost:8000** in your browser (Chrome/Edge recommended for recording).

Notes:

- **First transcription downloads the Whisper model automatically** (~150 MB for
  `base`). Later runs are offline.
- Without `ANTHROPIC_API_KEY` the app still fully works — recording, transcription,
  search, and the structured no-AI notes generator (flashcards included). A banner in
  the app reminds you AI notes are off.
- Change the Whisper model size with the `WHISPER_MODEL` environment variable
  (`tiny`, `base`, `small`, `medium` — `base`/`small` are the sweet spot on a 16 GB laptop).
- Change the Claude model with `LECTURELAB_MODEL` (default `claude-sonnet-4-6`).

## Workflow

1. **Create a course** in the sidebar, then a lecture inside it.
2. **Record** — big red button; pause/resume, elapsed timer, and a live level meter.
   While recording, tap **⭐ Important**, **❓ Confused — revisit**, or **📝 Exam hint**
   to drop timestamped markers.
3. **Stop** — audio uploads and transcribes locally with a progress bar.
4. **Fix the transcript** — click *Edit transcript* to correct mis-heard technical
   terms first (each course also has a 📖 custom-vocabulary list as a reminder).
5. **Generate notes** — AI (Claude) or the no-AI structured generator. You get
   outline, Markdown notes with LaTeX math, key terms, exam signals, 10–20 flashcards,
   an 8–12 question quiz, a 5-sentence summary, and explanations for every
   "Confused" marker.
6. **Study** — flip flashcards (space to flip, G/M for got/missed — missed cards
   resurface sooner via a Leitner queue), take the quiz (keys 1–4), export notes as
   Markdown, export flashcards as a tab-separated `.txt` for Anki, and search across
   every transcript and note (transcript hits jump the audio player to that moment).

## Data

Everything stays on your machine: `lecturelab.db` (SQLite) plus raw recordings in
`audio/`. Neither is committed to git.
