# LectureLab design

## Brief

- **Who:** one CS student, studying on a laptop and sometimes a phone, often late.
- **What:** recordings come in, transcripts come out, notes and flashcards follow.
- **The one job of the main screen:** show which lectures are ready to study from and how far along the rest are.

The product is made of recordings that have a length, transcripts tied to timestamps, and the stages a lecture moves through. The look is borrowed from the study desk: paper, ballpoint pen, highlighter.

## Color

Six named colors, plus one hairline. Defined as CSS variables in `static/index.html`.

| Token | Light | Dark | Role |
|---|---|---|---|
| `--paper` | #F7F8FA | #121418 | Page background. A cool white, on purpose not cream. |
| `--sheet` | #FFFFFF | #1A1D23 | Surfaces: dialog, player, inputs, hovered rows. |
| `--ink` | #16181D | #E8EAEE | Text, finished tapes. |
| `--pencil` | #5D6370 | #9AA1AE | Muted text, waiting tapes. 5.7:1 on paper (light), 7.1:1 (dark). |
| `--ballpoint` | #2747B8 | #8FA8FF | The only accent. Primary buttons, focus rings, links. White on it is 7.8:1. |
| `--highlighter` | #FFE45C | #E3C341 | Signature only: "happening now". Playhead region, current transcript line, transcribing tape and status. Always a background behind ink text, never a text color. |
| `--danger` | #B42318 | #F97066 | Delete, errors, failed batches. |
| `--rule` | #E3E6EC | #2C3038 | Hairlines and input borders. Not a color with a role, just structure. |

Rules:
- Ballpoint never decorates. If it isn't a primary action, a focus ring or a link, it's not ballpoint.
- Highlighter means "this is happening now" and nothing else.
- `color-scheme` follows the theme so native controls (audio player, scrollbars, date picker) match.

## Type

IBM Plex, self-hosted in `static/fonts/` (SIL OFL, see `static/fonts/LICENSE.txt`). One family in three cuts:

- **Display:** Plex Sans Condensed 600. Page titles, section heads, course codes, the wordmark. Reads like a course catalog.
- **Body:** Plex Sans 400 and 600. Everything else, including the transcript.
- **Mono:** Plex Mono 400. Only timestamps, durations and file paths, because they really are tabular or technical. Not for decoration or random small labels.

Scale (px): 13 / 15 / 17 / 22 / 34.

| Size | Use |
|---|---|
| 34 | Page title (condensed 600, `text-wrap: balance`) |
| 22 | Section head (condensed 600) |
| 17 | Lecture titles in the list, transcript text (line-height 1.6, max 68ch) |
| 15 | Body, buttons, inputs |
| 13 | Meta, hints, statuses |

## Spacing

4px grid: 4, 8, 12, 16, 24, 32, 48. Tight inside a lecture row (4 to 8), wide between groups (32 to 48). Things that belong together sit close; sections are separated by space and a rule, not by boxes.

## Layout

A narrow course rail beside a list of lectures. Lectures sit on rule lines, not in cards. On phones the rail collapses to a top bar with one scrolling row of course chips, so the Library is the first thing you see.

```
┌ rail ────────┬────────────────────────────────────────────┐
│ LectureLab   │ Library              [Transcribe 3 waiting]│
│ [Add rec.]   │ ───────────────────────────────────────────│
│ Library      │ reliable data transfer part 1      Oct 13  │
│ Courses      │ CNT4007  ▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭▭  52:24  waiting  │ <- tape
│              │ ───────────────────────────────────────────│
│ CIS4930      │ Discussion 1                       Aug 21  │
│ CNT4007      │ PHI3681  ██████  14:03  transcribed        │
└──────────────┴────────────────────────────────────────────┘

Lecture page
┌────────────────────────────────────────────┐
│ Lecture 1                       transcribed │
│ [ video or audio player, max 50vh ]         │
│ ▕▏▏▏▏▏▏▏▏▏▏███▏▏▏▏▏▏▏▏▏▏▏ 2:13 / 28:33     │ <- tape, sticky scrubber
│ 0:02  You'll be fine.                       │
│ 0:03  And I will be the instructor and I    │
│       will be talking to you today ...      │ <- hanging timestamps
└────────────────────────────────────────────┘
```

## Signature: the tape

A thin strip per lecture whose length is proportional to its duration, so a 52 minute lecture is visibly longer than a 14 minute one. Its texture shows where the transcript is:

- **Waiting:** a thin dotted pencil line (dashed on the scrubber).
- **Transcribing:** highlighter, with a slow sweep. Holds still under `prefers-reduced-motion`.
- **Transcribed (and every later stage):** solid ink.
- **Error:** danger. **Possible duplicate:** hatched pencil.

On the lecture page the same tape becomes the scrubber: one tick per transcript segment, the played part highlighted, an ink playhead, click or arrow keys to seek. It stays sticky while you read the transcript, instead of the video: a sticky 42vh video used to cover half the screen and the focused line. The video is capped at 50vh and scrolls away, because slides need to stay readable.

Everything else stays quiet so the tape is the thing you remember.

## Motion

- Only `transform` and `opacity`. No color transitions; hover changes are instant.
- Entrances: 180ms `cubic-bezier(.2, .8, .2, 1)`.
- The only ambient motion is the transcribing sweep and job indicators. All of it stops under reduced motion.

## States

- **Hover** raises contrast: primary buttons darken, ghost buttons get a pencil border, rows get the sheet background.
- **Focus:** 2px ballpoint ring via `:focus-visible`, 2px offset.
- **Active:** buttons press down 1px.
- **Disabled:** 45% opacity, `not-allowed` cursor, no hover change, and nearby text says why.
- **Loading:** skeletons shaped like the view, shown only if a fetch takes longer than 120ms.
- **Empty:** says what goes here and gives the action (Add recording, plus the course's inbox path).
- **Error:** says what happened and how to fix it, with Retry or Back to Library. Client-side form errors appear inline under the field and take focus; server errors arrive as toasts.

## Keyboard

- `A`: Add recording.
- `K`: play or pause. `J` / `L`: back or forward 10 seconds (lecture page).
- Tape scrubber: Left/Right 5 seconds, Home/End.
- Shortcuts are ignored while typing or while a dialog is open.

## Removed on purpose

The 🎓 emoji wordmark, the tracked all-caps "COURSES" label, cards around every block, and most "A · B · C" strings.
