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

## Slides

The words and the slide they were said over belong together, so the transcript is sectioned by slide rather than given a separate gallery.

- **Markers:** where the slide changes, a small "Slide 7" label and the slide's title sit in the timestamp column's rhythm. A slide the lecturer goes back to gets a marker again. Markers come from `segments[].slide_no`, so they're exactly as good as the alignment.
- **Panel (1280px and wider):** the current slide sits beside the transcript and stays in view under the tape. It follows the line being played, or the top line on screen while paused. Clicking it opens the viewer. The page widens to 1240px only when the panel is there.
- **Narrower screens:** no panel. Each marker shows a 240px thumbnail instead.
- **No slide on screen:** in an aligned lecture, a stretch where the video shows something else (a live coding demo, say) gets a quiet "No slide" marker, and the panel shows a same-size placeholder instead of the last slide.
- **Viewer:** full-size slide, Previous/Next (arrow keys), "Play from" its first appearance, and "Also on screen at" for every later stretch. The open slide is in the URL (`?slide=7`), so a link opens the viewer on it.
- **Slides section:** says what's going on in plain words (none, waiting, aligning, aligned, error), shows how the deck was matched, and lets you fix it: pick another deck from the course, choose no slides, or attach a PDF or PPTX. Picking in the list only selects; "Use this deck" commits it, and "Remove slides" asks first. Errors from an upload appear under the attach button.
- Thumbnails reserve the deck's aspect ratio before they load. A missing image becomes a dashed placeholder of the same size with the slide's title.
- No new color: the aligning row reuses the jobs sweep (highlighter, because it's happening now). Slides add nothing to the tape.

## Notes

The notes are a shorter way to read the same lecture, so they borrow the transcript's shape instead of looking like a document.

- **Tabs:** Notes and Transcript under the tape, in the URL as `?tab=`. Notes is open by default once notes exist. Slides, Details and Sources stay below both.
- **The gutter:** paragraphs that start with a jump link hang it in the transcript's 56px timestamp column, so each point sits beside the moment it was said. Click it to play from there. Below 760px the gutter goes and the time leads the paragraph instead.
- **Sections:** the start time in the gutter, the title (22, condensed), then "Class logistics" and the slide range ("Slides 7 to 9 and 12", which opens the viewer once slides are lined up), then the one-line gist in pencil. `?section=3` opens the notes at section 3.
- **Notches:** each section start is a 2px ink mark across the tape scrubber, taller than the tape, so it never reads as a pause in speech. This is the tape learning the shape of the lecture.
- **Contents:** "In this lecture", with times. At 1280px and wider it sits beside the notes, stays in view and marks the section being read.
- **Callouts:** told apart by the left rule and a label, not by color. On the exam: solid ink. Emphasized in class: solid pencil. Unclear in the recording: dashed pencil. Check this (added by the fact check): dashed danger.
- **Code:** sheet background, rule border, mono 13, scrolls sideways with keyboard focus; the language name sits top right. Inline code is a faint ink tint, not a box, so code-heavy paragraphs stay calm.
- **Math:** KaTeX, loaded from the CDN only when a note has math. Without it the TeX source stays readable.
- **What the check changed:** collapsed at the end. Removed claims are struck through, with where they were and why.
- **States:** none (what notes are and the two ways to write them), queued and writing (the jobs sweep, with live progress), Claude's usage limit (when it resumes, in local time, plus "Write offline notes instead"), error, and out of date (the server's reason plus "Write the notes again"). Offline notes over Claude's ask before replacing them.
- Everything from the server is escaped first. Only its Markdown conventions are rendered.

## Review

Flashcards from every lecture's notes, one at a time, in Leitner boxes (back in 1, 2, 4, 8 or 16 days).

- **Where:** "Review" in the rail with the number due. It stays hidden on a server without flashcards. `#/review` takes `?course=1` or `?lecture=22`, and "Cards from" changes it. The notes footer links its flashcard count here.
- **The card:** the one real card in the app, because it is one. Sheet surface, rule border, 68ch. On top: course code, the lecture (a link), "New card" or "Box 2", and how many are left. The question is 22 sans, not condensed, so long questions stay readable.
- **Cloze:** the blank is an underlined gap in pencil, or the hint if the card has one. Revealed, it's the answer in ink 600 on an ink underline. Parsed like Anki, so `{{c1::std::vector}}` means answer "std", hint "vector": the card writer must not put `::` inside an answer.
- **Card text:** the same escaped Markdown as the notes, math included. Jump links are dropped, since they only work on the lecture page; "Hear it in the lecture" covers that.
- **Answer:** below a dashed rule, plus "Hear it in the lecture at 10:38", which opens the lecture at that moment.
- **Boxes:** five short rules with counts, the same 3px rhythm as the tape. The current card's box is drawn in ink. On phones the labels shorten to "2 days".
- **Keys:** Space shows the answer, 1 missed it, 2 got it, U undo. Each button shows its key, hidden on touch screens and from screen readers, which get `aria-keyshortcuts`.
- **Undo:** each answer saves straight away and the next card shows at once. A line under the card says when the card comes back, with Undo. A save that fails puts the card back and says why.
- **States:** loading skeleton shaped like the page, no cards yet (where they come from), nothing due, done (how many you answered), and no flashcards on this server.
- **Anki:** "Download these flashcards for Anki" under the card follows the scope. The notes footer has the same download for its lecture.

## Quiz

One lecture's questions on one page, because a quiz is short and you want to see how you did across all of it.

- **Where:** `#/quiz/22`, from the quiz count in the lecture's notes footer. The head says how many questions, which lecture (a link), and how you did last time.
- **Questions:** on rule lines, like lectures, with their number in the gutter. Multiple choice is a list of full-width choices (44px or taller, the whole row clicks). Written answers get a text box.
- **Check my answers:** marks multiple choice without saving anything. The answers lock (written ones go dashed), and a summary at the top takes focus: how many right so far, and what's left to mark.
- **Results:** told by a word and a left rule, never by color alone. Right: solid ink. Wrong: solid danger. Not marked yet: dashed pencil. The right choice says "Right answer"; yours says "Your answer", or both. Then the explanation and "Hear it in the lecture at 5:31", which opens in a new tab so the checked quiz stays put.
- **Written answers:** the model answer, then "I got it" and "I missed it" (pressed shows ink; press again to unmark). A blank answer is simply "Not answered" and counts as missed, with nothing to mark. Marks lock once the attempt is saved.
- **Save this attempt:** records one attempt. Unmarked written answers count as wrong, and the line beside the button says so first. While a check or save is out, every button that could start another is off. Afterwards "Saved: 9 of 12" and "Take it again", which clears the answers.
- **Change my answers:** before saving, unlocks the answers you gave (they stay) and drops the marks.
- **Leaving:** warns only when there's something to lose: answers typed but not checked, or your own marks not saved. Checking and walking away is fine.
- **States:** loading skeleton, no quiz yet (go to the notes), no quizzes on this server, check or save errors under the button (your answers stay, focus returns to the button).

## Search

What was said, the notes and the slides, across every lecture.

- **Where:** "Search" in the rail (hidden on a server without it), or `/` from anywhere. `#/search?q=…&course=1`, so a search is a link.
- **As you type:** waits for a 250ms pause, and cancels the request before it. The old results dim while the new ones load.
- **Results:** grouped by lecture (title, course, date), each hit a row in the transcript's rhythm: time, then what it is (Said, Notes, Slide), then the snippet. A notes hit opens the Notes tab there; the others open the transcript at that moment.
- **Matches:** ink 600 on an ink underline, never highlighter, which stays "happening now". Next to each other, matched words read as one match.
- **Escaping:** the server escapes snippets and adds `<mark>`. The page drops any other tag.
- **States:** before typing (what's searched and how matching works), nothing matches (try fewer words), error (the server's reason), and no search on this server.

## Setup

For someone who just installed LectureLab, and for you when a setting changes. Its one job: get from a fresh install to the first recording, and show what's ready and what isn't.

- **Where:** `#/setup`, "Setup" at the end of the rail. A fresh install (no courses, no lectures) opens here instead of the Library.
- **The rows are the path a lecture takes:** Courses, Canvas, Recordings, Transcripts, Notes, Saved to. They're on rule lines like lectures, not cards, and not numbered: the order is the point, so it's shown by the tape.
- **The tape, bent into a line:** a 4px vertical segment beside each row, in the tape's textures. Solid ink: ready. Dotted pencil: not set up, or optional. Danger: broken, with the fix beside it. Highlighter: being checked right now. A word always says the same thing as the texture, so color is never the only signal: "Ready" or "Connected" (solid), "Add one", "Not checked", "Not connected", "Optional" or "Off" (dotted), "Fix this" or "Sign in again" (danger), "Checking…" (highlighter). No checkmarks, no "4 of 6", no progress bar.
- **Each row:** name (17, 600), a one-line pencil description of what that stage does, the status word at the right, then whatever it needs: a field and a button, or a path in mono.
- **Courses:** the list with Delete (asks first; a course with lectures says why it can't go). After a delete, focus moves to the next Delete, so a phone doesn't pop its keyboard. An add form for code and name. Once Canvas is connected, "Show my Canvas courses" lists the ones not added yet, each with "Add" and its suggested code.
- **Canvas:** the address and "Save address", then "Connect Canvas", which opens LectureLab's own browser. The password is never typed into this page. A value from `.env` disables the field and says so.
- **Notes:** "Check Claude's sign-in" makes one tiny real call, so it only runs when pressed. The result stays on the row.
- **The end:** "Add your first recording" (or "Go to the Library" once there are lectures). It stays off until there's a course, ffmpeg is found and the notes folder can be written, and the line beside it says which of those is missing.
- **Solid only where something's missing:** ballpoint goes to the button that fixes a missing step: "Add course" with no courses, "Save address" with no address, "Connect Canvas" or "Sign in to Canvas" when that's what's missing, and "Add your first recording". Once a step is ready its button turns ghost, so a set-up page has none. Everything else is always ghost: "Check Claude's sign-in" always, because it spends a little of your plan and shouldn't look like the thing to press, and "Go to the Library" once there are lectures.
- **A Canvas course with no code in its name:** "Add" puts its name in the form and asks for a code. Change the name before adding and it's added without the Canvas link, and the toast says so.
- **Verbs carry through:** "Save address" ends in "Address saved", "Add course" in "Added COP4020", "Delete" in "Deleted COP4020". Field errors go under their field and take focus.
- **States:** a loading skeleton of six rows, and the shared error view. There's no empty state, because every row always has something to say.

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
- `/`: Search.
- `K`: play or pause. `J` / `L`: back or forward 10 seconds (lecture page).
- Slide viewer: Left/Right for the previous or next slide.
- Lecture tabs: Left/Right, Home/End.
- Review: Space shows the answer, 1 missed it, 2 got it, U undoes the last answer.
- Tape scrubber: Left/Right 5 seconds, Home/End.
- Shortcuts are ignored while typing or while a dialog is open.

## Removed on purpose

The 🎓 emoji wordmark, the tracked all-caps "COURSES" label, cards around every block, and most "A · B · C" strings.
