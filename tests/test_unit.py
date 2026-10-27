import importlib
import subprocess
import sys

import pytest


@pytest.fixture()
def ll(tmp_path, monkeypatch):
    """Fresh lecturelab module pointed at a throwaway data dir."""
    monkeypatch.setenv("LECTURELAB_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("LECTURELAB_LOG", str(tmp_path / "test.log"))
    import lecturelab
    return importlib.reload(lecturelab)


def test_courses_seeded_once(ll):
    codes = [c["code"] for c in ll.list_courses()]
    assert codes == sorted(["COP4020", "COP4533", "CNT4007", "CIS4930", "PHI3681", "MML"])
    ll._db_ready = False
    ll.init_db()  # re-running init must not duplicate or reset anything
    assert len(ll.list_courses()) == 6


def test_vocab_update_normalizes_lines(ll):
    cop = next(c for c in ll.list_courses() if c["code"] == "COP4020")
    out = ll.update_course(cop["id"], vocabulary="  monad \n\n\nfunctor\r\n  ")
    assert out["vocabulary"] == "monad\nfunctor"


def test_deleted_seed_course_stays_deleted(ll):
    with ll.db() as con:
        con.execute("DELETE FROM courses WHERE code='CIS4930'")
    ll._db_ready = False
    ll.init_db()
    assert "CIS4930" not in [c["code"] for c in ll.list_courses()]


def test_empty_course_name_rejected(ll):
    cop = next(c for c in ll.list_courses() if c["code"] == "COP4020")
    with pytest.raises(ValueError):
        ll.update_course(cop["id"], name="   ")


def test_filelock_freed_when_holder_dies(ll, tmp_path):
    path = tmp_path / "y.lock"
    holder = subprocess.Popen([sys.executable, "-c",
                               f"import os, lecturelab as ll; assert ll.FileLock(r'{path}').acquire(); os._exit(0)"])
    assert holder.wait() == 0  # exited without release()
    assert ll.FileLock(path).acquire()


def test_task_script_runs_hidden_and_restarts():
    import cli
    s = cli.server_task_script()
    assert "pythonw.exe" in s and "supervise" in s
    assert "-RestartCount 999" in s and "-Priority 4" in s and "-AtLogOn" in s
    assert cli._q("C:/it's here") == "'C:/it''s here'"


def test_filelock_excludes_other_process(ll, tmp_path):
    path = tmp_path / "x.lock"
    lock = ll.FileLock(path)
    assert lock.acquire()
    assert not ll.FileLock(path).acquire()  # same process, second holder
    probe = ("import sys, lecturelab as ll; "
             f"sys.exit(0 if ll.FileLock(r'{path}').acquire() else 3)")
    assert subprocess.run([sys.executable, "-c", probe]).returncode == 3  # other process blocked
    lock.release()
    assert subprocess.run([sys.executable, "-c", probe]).returncode == 0  # freed after release


def test_parse_date_sources(ll):
    assert ll.parse_date(None, "20260821") == "2026-08-21"  # yt-dlp upload_date
    assert ll.parse_date("2026-04-08 19.47.47 Jonathan Kashi's Zoom Meeting") == "2026-04-08"
    assert ll.parse_date("2026-13-40T00:00", "Lecture_2026_09_03.mp4") == "2026-09-03"  # invalid first, valid second
    assert ll.parse_date("no date here") is None


def test_classify_url(ll):
    assert ll.classify_url("https://drive.google.com/file/d/abc/view") == "drive"
    assert ll.classify_url("https://ufl.zoom.us/rec/share/xyz") == "zoom"
    assert ll.classify_url("https://ufl.instructure.com/courses/1/files/2") == "canvas"
    assert ll.classify_url("https://www.youtube.com/watch?v=x") == "url"


def test_envelope_match_tolerates_offset_but_not_different_audio(ll):
    import numpy as np
    rng = np.random.default_rng(0)
    talk = rng.normal(-1.5, 0.4, 900).astype(np.float16)  # 15 min of "speech" loudness
    trimmed = talk[7:].copy()  # same lecture, trimmed 7 s, slightly different encode
    trimmed += rng.normal(0, 0.02, trimmed.size).astype(np.float16)
    other = rng.normal(-1.5, 0.4, 900).astype(np.float16)
    assert ll.envelope_match(talk.tobytes(), trimmed.tobytes()) > 0.95
    assert ll.envelope_match(talk.tobytes(), other.tobytes()) < 0.5
    silence = np.full(900, -4, np.float16)
    assert ll.envelope_match(silence.tobytes(), silence.tobytes()) == 0.0


V1_DB = __import__("pathlib").Path.home() / "Documents/Projects/lecturelab/lecturelab.db"


@pytest.mark.skipif(not V1_DB.exists(), reason="v1 database not on this machine")
def test_import_v1_brings_both_lectures_and_is_rerunnable(ll):
    first = ll.import_v1(V1_DB)
    assert [r.get("skipped") for r in first] == [None, None]
    by_title = {ll.get_lecture(r["lecture_id"])["title"]: ll.get_lecture(r["lecture_id"]) for r in first}
    assert by_title["Lecture 1"]["course_code"] == "COP4020"  # its audio link was NULL in v1; relinked
    assert by_title["Discussion 1"]["course_code"] == "PHI3681"
    for lec in by_title.values():
        assert lec["segment_count"] > 50 and lec["transcript_source"] == "v1"
        assert lec["status"] == "queued_transcribe"  # still gets a large-v3 transcript
        assert (ll.DATA / lec["audio_path"]).exists()
    again = ll.import_v1(V1_DB)
    assert all(r["duplicate"] for r in again)
    assert len(ll.list_lectures()) == 2


def test_scan_html_finds_real_embed_shapes(ll):
    html = """
    <p><iframe src="https://ufl.instructure.com/courses/5/external_tools/retrieve?display=borderless&amp;url=https%3A%2F%2F2375821.kaf.kaltura.com%2Fbrowseandembed%2Findex%2Fmedia%2Fentryid%2F1_ab12cd34%2FshowDescription%2Ffalse"></iframe></p>
    <iframe src="https://ufl.hosted.panopto.com/Panopto/Pages/Embed.aspx?id=0f8fad5b-d9cb-469f-a165-70867728950e&autoplay=false"></iframe>
    <a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ">video</a>
    <a href="https://ufl.zoom.us/rec/share/AbC-123_xyz.Q9">recording</a>
    <iframe src="/media_objects_iframe/m-4uoGqVdEqXhpqu2ZMytHgy9XRV4xNkB"></iframe>"""
    found = {f["key"]: f for f in ll.scan_html_for_media(html, "page: Week 1")}
    assert found["kaltura:1_ab12cd34"]["url"] == "kaltura:2375821:1_ab12cd34"
    assert found["panopto:0f8fad5b-d9cb-469f-a165-70867728950e"]["url"] ==         "https://ufl.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=0f8fad5b-d9cb-469f-a165-70867728950e"
    assert "youtube:dQw4w9WgXcQ" in found
    assert any(k.startswith("zoom:https://ufl.zoom.us/rec/share/") for k in found)
    assert found["media_object:m-4uoGqVdEqXhpqu2ZMytHgy9XRV4xNkB"]["page_url"] == "page: Week 1"


def test_canvas_without_token_explains_how_to_get_one(ll, monkeypatch):
    monkeypatch.setattr(ll, "load_dotenv", lambda *a, **k: None)
    monkeypatch.delenv("CANVAS_TOKEN", raising=False)
    with pytest.raises(ll.CanvasError, match="New Access Token"):
        ll.canvas_config()


# ---- ingest robustness (review findings)

@pytest.fixture()
def noise(ll, tmp_path):
    """A 70 s audio file for plumbing tests (fingerprints need at least 60 s)."""
    out = tmp_path / "noise.wav"
    subprocess.run([ll.find_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "anoisesrc=d=70:c=pink:a=0.3,volume='if(lt(mod(t,7),3),1,0.2)':eval=frame",
                    "-ac", "1", "-ar", "16000", str(out)], check=True)
    return out


def test_failed_finalize_rolls_back_so_retry_works(ll, noise, monkeypatch):
    real = ll._move_into_place
    monkeypatch.setattr(ll, "_move_into_place", lambda *a: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(PermissionError):
        ll.ingest_file(noise, kind="file")
    assert ll.list_lectures() == []  # no poisoned half-row left behind
    monkeypatch.setattr(ll, "_move_into_place", real)
    r = ll.ingest_file(noise, kind="file")
    assert not r["duplicate"] and ll.get_lecture(r["lecture_id"])["status"] == "queued_transcribe"


def test_ids_never_reused_and_leftover_dirs_cleared(ll, noise, tmp_path):
    first = ll.ingest_file(noise, kind="file")["lecture_id"]
    ll.delete_lecture(first)
    (ll.DATA / "media" / str(first + 1)).mkdir(parents=True)  # a leftover the next id would hit
    (ll.DATA / "media" / str(first + 1) / "junk").write_text("x")
    second = ll.ingest_file(noise, kind="file")["lecture_id"]
    assert second == first + 1
    assert not (ll.DATA / "media" / str(second) / "junk").exists()


def test_exact_reingest_of_parked_duplicate_does_not_park_again(ll, noise, tmp_path):
    a = ll.ingest_file(noise, kind="file")["lecture_id"]
    mp3 = tmp_path / "copy.mp3"
    subprocess.run([ll.find_tool("ffmpeg"), "-v", "error", "-i", str(noise), "-b:a", "64k", str(mp3)], check=True)
    parked = ll.ingest_file(mp3, kind="file")
    assert parked["parked_dup_of"] == a
    again = ll.ingest_file(mp3, kind="file")
    assert again["duplicate"] and again["lecture_id"] == parked["lecture_id"]
    assert len(ll.list_lectures()) == 2


def test_corrupt_audio_raises_instead_of_storing_bad_signature(ll, tmp_path):
    bad = tmp_path / "bad.flac"
    bad.write_bytes(b"fLaC" + b"\x00" * 5000)
    with pytest.raises(RuntimeError, match="could not decode"):
        ll.pcm_signature(bad)


def test_claim_job_hands_each_job_out_once(ll):
    import threading
    ids = [ll.enqueue("noop", {}) for _ in range(20)]
    got, lock = [], threading.Lock()

    def grab():
        while (j := ll.claim_job(["noop"])):
            with lock:
                got.append(j["id"])
    ts = [threading.Thread(target=grab) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(got) == ids


def test_orphaned_jobs_requeued_then_given_up(ll):
    jid = ll.enqueue("noop", {})
    with ll.db() as con:
        con.execute("UPDATE jobs SET status='running', owner_pid=999999, attempts=1 WHERE id=?", (jid,))
    live = ll.enqueue("noop", {})
    with ll.db() as con:
        con.execute("UPDATE jobs SET status='running', owner_pid=? WHERE id=?", (__import__("os").getpid(), live))
    ll.requeue_orphaned_jobs()
    assert ll.get_job(jid)["status"] == "queued" and ll.get_job(live)["status"] == "running"
    with ll.db() as con:
        con.execute("UPDATE jobs SET status='running', owner_pid=999999, attempts=3 WHERE id=?", (jid,))
    ll.requeue_orphaned_jobs()
    assert ll.get_job(jid)["status"] == "error"


def test_inbox_waits_for_stable_file_and_retries_failures(ll, noise, tmp_path, monkeypatch):
    import shutil
    monkeypatch.setattr(ll, "LOCAL_INBOX", tmp_path / "inbox")
    monkeypatch.setenv("LECTURELAB_DRIVE_INBOX", "off")
    ll.scan_inboxes()  # creates course folders
    shutil.copy(noise, tmp_path / "inbox" / "COP4020" / "lec.wav")
    assert ll.scan_inboxes() == []  # first sighting: wait for a stable size
    [jid] = ll.scan_inboxes()
    assert ll.get_job(jid)["payload"]["course_id"] == ll.find_course("COP4020")["id"]
    assert ll.scan_inboxes() == []  # unchanged and its job hasn't failed: leave it
    ll.job_update(jid, status="error", error="boom")
    [retry] = ll.scan_inboxes()  # failed job: try again
    assert retry != jid


def test_canvas_same_embed_in_two_courses_kept_for_both(ll, monkeypatch):
    page = {"body": '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>', "title": "Wk1"}
    def fake_get(path, params=None, paginate=True):
        if path.endswith("/pages"):
            return [{"url": "wk1"}]
        if "/pages/" in path:
            return page
        return [] if paginate else None
    monkeypatch.setattr(ll, "canvas_get", fake_get)
    a, b = ll.find_course("COP4020"), ll.find_course("COP4533")
    for c, cid in ((a, 11), (b, 22)):
        c["canvas_id"] = cid
        assert ll.canvas_scan_course(c)["new"] == 1
    with ll.db() as con:
        assert con.execute("SELECT COUNT(*) FROM canvas_items WHERE key='youtube:dQw4w9WgXcQ'").fetchone()[0] == 2


def test_source_urls_drop_zoom_passcode(ll):
    assert ll.clean_source_url("https://ufl.zoom.us/rec/share/abc?pwd=s3cret&startTime=1") == \
        "https://ufl.zoom.us/rec/share/abc?startTime=1"


def test_utc_timestamps_use_local_date(ll):
    # 2026-09-03 01:30 UTC is the evening of Sep 2 in Gainesville (UTC-4 in September);
    # whatever this machine's zone is, the stored date must be the local one.
    from datetime import datetime, timezone
    expected = datetime(2026, 9, 3, 1, 30, tzinfo=timezone.utc).astimezone().date().isoformat()
    assert ll.parse_date("2026-09-03T01:30:00Z") == expected


def test_api_upload_rejects_bad_names_and_media_404(ll):
    from fastapi.testclient import TestClient
    c = TestClient(ll.create_app(), base_url=f"http://127.0.0.1:{ll.PORT}")
    r = c.post("/api/recordings/upload", files={"file": ("notes.exe", b"x")})
    assert r.status_code == 400
    r = c.post("/api/recordings/upload", files={"file": (r"..\..\CON.mp4", b"x")}, data={"course_id": "999"})
    assert r.status_code == 400 and "no course" in r.text
    assert c.get("/api/lectures/1/media/..%2F..%2Fx").status_code == 404
    with ll.db() as con:
        con.execute("INSERT INTO lectures(title, status, audio_path) VALUES ('t','queued_transcribe','media/9/audio.flac')")
    assert c.get("/api/lectures/1/media/audio").status_code == 404  # row points at a missing file
    assert c.get("/api/health", headers={"host": "evil.example"}).status_code == 403
