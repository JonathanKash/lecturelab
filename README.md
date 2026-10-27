# LectureLab

Turns real lecture recordings into notes, flashcards and quizzes you can study from instead of rewatching. It costs nothing beyond a Claude Max plan and Google AI Pro.

This is v2, a rebuild that's still in progress. The full setup guide lands when it's done. v1 (the browser recorder) is at the `v1-archive` tag.

## Quick start (so far)
```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\lecturelab serve
```
Then open http://127.0.0.1:8765.
