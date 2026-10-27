"""Runs INSIDE a Colab GPU runtime. LectureLab uploads it to /content/lecturelab/transcribe.py and
starts it as a child process (see COLAB_LAUNCHER in lecturelab.py), so a native crash is reported.

The batch runner first uploads, into /content/lecturelab/:
    manifest.json   {"model": "large-v3", "items": [{"lecture_id": 7, "audio": "7.flac",
                                                     "initial_prompt": "..."}]}
    <id>.flac       16 kHz mono audio, one per lecture

For each item this writes out/<lecture_id>.json as soon as that lecture is done, and skips
items whose output already exists, so a re-run after an interruption only does what's left.
Everything comes from the manifest; there are no arguments.
"""
import json
import os
import subprocess
import sys
import time

WORK = "/content/lecturelab"
OUT = os.path.join(WORK, "out")
FASTER_WHISPER = "faster-whisper==1.2.1"  # pinned: same version as the local fallback


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def ensure_faster_whisper():
    try:
        import faster_whisper  # noqa: F401
        return
    except ImportError:
        pass
    t = time.time()
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", FASTER_WHISPER], check=True)
    log(f"installed {FASTER_WHISPER} in {time.time() - t:.0f}s")


def load_audio(path):
    """Decode with ffmpeg rather than PyAV: some PyAV releases break faster-whisper's decoder."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def main():
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(WORK, "manifest.json")) as f:
        manifest = json.load(f)
    todo = [it for it in manifest["items"] if not os.path.exists(os.path.join(OUT, f"{it['lecture_id']}.json"))]
    log(f"{len(todo)} of {len(manifest['items'])} lecture(s) to transcribe")
    if not todo:
        return
    ensure_faster_whisper()
    import torch  # Colab ships it; used only to name the GPU in the output
    from faster_whisper import WhisperModel

    gpu = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none"
    t = time.time()
    model = WhisperModel(manifest.get("model", "large-v3"), device="cuda", compute_type="float16")
    log(f"loaded {manifest.get('model', 'large-v3')} on {gpu} in {time.time() - t:.0f}s")

    for it in todo:
        lid = it["lecture_id"]
        t = time.time()
        audio = load_audio(os.path.join(WORK, it["audio"]))
        segments, info = model.transcribe(
            audio,
            language=it.get("language", "en"),
            beam_size=5,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            word_timestamps=True,
            initial_prompt=it.get("initial_prompt") or None,
            condition_on_previous_text=True,
        )
        segs, beat = [], time.time()
        for s in segments:
            if time.time() - beat > 60:  # a heartbeat, so a long lecture never looks hung
                log(f"lecture {lid}: {s.end / 60:.1f} of {info.duration / 60:.1f} min")
                beat = time.time()
            segs.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip(),
                         "words": [[round(w.start, 2), round(w.end, 2), w.word, round(w.probability, 3)]
                                   for w in (s.words or [])]})
        took = time.time() - t
        result = {"lecture_id": lid, "model": manifest.get("model", "large-v3"), "device": gpu,
                  "language": info.language, "duration": round(info.duration, 2),
                  "elapsed_sec": round(took, 1), "segments": segs}
        tmp = os.path.join(OUT, f"{lid}.json.tmp")
        with open(tmp, "w") as f:
            json.dump(result, f)
        os.replace(tmp, os.path.join(OUT, f"{lid}.json"))  # never leave a half-written result
        log(f"lecture {lid}: {info.duration / 60:.1f} min of audio in {took:.0f}s "
            f"({info.duration / max(took, 1e-6):.0f}x realtime), {len(segs)} segments")
    log("batch done")


if __name__ == "__main__":
    main()
