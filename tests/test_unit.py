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


class FakeResponse:
    def __init__(self, status, body, ctype="application/json", link=None):
        self.status, self.body = status, body
        self.headers = {"content-type": ctype, **({"link": link} if link else {})}
        self.ok = 200 <= status < 300

    def text(self):
        return self.body if isinstance(self.body, str) else __import__("json").dumps(self.body)

    def json(self):
        return self.body


def fake_session(ll, responses):
    """A CanvasSession whose browser context answers from a list, recording the URLs asked for."""
    cs = ll.CanvasSession()
    asked = []

    class Req:
        def get(self, url, **kw):
            asked.append(url)
            return responses.pop(0)
    cs.ctx = type("Ctx", (), {"request": Req()})()
    return cs, asked


def test_canvas_api_paginates_and_treats_404_as_empty(ll):
    nxt = '<https://ufl.instructure.com/api/v1/courses?page=2&per_page=100>; rel="next"'
    cs, asked = fake_session(ll, [FakeResponse(200, [{"id": 1}], link=nxt), FakeResponse(200, [{"id": 2}]),
                                  FakeResponse(404, {"errors": []})])
    assert cs.get("courses", {"include[]": ["term", "teachers"]}) == [{"id": 1}, {"id": 2}]
    assert "include%5B%5D=term&include%5B%5D=teachers" in asked[0] and asked[1].endswith("page=2&per_page=100")
    assert cs.get("courses/9/files") == []  # Files tab hidden: not an error
    with pytest.raises(ll.CanvasError, match="non-API"):
        cs.get("https://evil.example/api/v1/x")


def test_expired_session_marks_needs_signin_and_notifies_once(ll, monkeypatch):
    sent = []
    monkeypatch.setattr(ll, "notify", lambda t, b: sent.append(t))
    ll.set_canvas_state("connected", user="Kashi")
    for resp in (FakeResponse(401, {"status": "unauthenticated"}),
                 FakeResponse(302, "", ctype="text/html"),  # bounced to the GatorLink login page
                 FakeResponse(200, "<html>login</html>", ctype="text/html")):
        cs, _ = fake_session(ll, [resp])
        with pytest.raises(ll.CanvasNeedsSignIn):
            cs.get("users/self", paginate=False)
    assert ll.canvas_state()["state"] == "needs_signin"
    assert len(sent) == 1  # one notification per expiry, not one per request


def test_signin_polling_is_quiet(ll, monkeypatch):
    sent = []
    monkeypatch.setattr(ll, "notify", lambda t, b: sent.append(t))
    cs, _ = fake_session(ll, [FakeResponse(401, {})])
    with pytest.raises(ll.CanvasNeedsSignIn):
        cs.get("users/self", paginate=False, quiet_auth=True)
    assert ll.canvas_state()["state"] == "not_connected" and not sent


def test_needs_signin_never_opens_the_browser(ll, monkeypatch):
    ll.meta_set("canvas_state", {"state": "needs_signin"})
    monkeypatch.setattr(ll, "CanvasSession", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened browser")))
    with pytest.raises(ll.CanvasNeedsSignIn):
        ll.canvas_get("courses")
    with pytest.raises(ll.CanvasNeedsSignIn):
        ll.canvas_ready()


def test_netscape_cookie_export(ll):
    text = ll.netscape_cookies([
        {"name": "canvas_session", "value": "abc", "domain": "ufl.instructure.com", "path": "/", "secure": True,
         "expires": -1},
        {"name": "kms_ks", "value": "x=1", "domain": ".kaltura.com", "path": "/", "secure": False,
         "expires": 1893456000.5}])
    lines = text.splitlines()
    assert lines[0] == "# Netscape HTTP Cookie File"
    assert lines[2].split("\t") == ["ufl.instructure.com", "FALSE", "/", "TRUE", "0", "canvas_session", "abc"]
    assert lines[3].split("\t") == [".kaltura.com", "TRUE", "/", "FALSE", "1893456000", "kms_ks", "x=1"]


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
    monkeypatch.setattr(ll, "CanvasSession", lambda *a, **k: __import__("contextlib").nullcontext())  # no browser
    ll.set_canvas_state("connected", user="Kashi")
    a, b = ll.find_course("COP4020"), ll.find_course("COP4533")
    for c, cid in ((a, 11), (b, 22)):
        c["canvas_id"] = cid
        assert ll.canvas_scan_course(c)["new"] == 1
    with ll.db() as con:
        rows_ = con.execute("SELECT page_link FROM canvas_items WHERE key='youtube:dQw4w9WgXcQ'").fetchall()
    assert [r[0] for r in rows_] == ["https://ufl.instructure.com/courses/11/pages/wk1",
                                     "https://ufl.instructure.com/courses/22/pages/wk1"]  # opened before a pull


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


def test_orphan_sweep_skips_while_a_batch_runs(ll, monkeypatch):
    calls = []
    monkeypatch.setattr(ll, "stop_orphan_sessions", lambda keep=None: calls.append(1) or [])
    batch = ll.FileLock(ll.DATA / "transcribe.lock")
    assert batch.acquire()  # a batch is running (in this or another process)
    ll.sweep_orphans_if_idle()
    assert calls == []  # must not stop the session that batch is using
    batch.release()
    ll.sweep_orphans_if_idle()
    assert calls == [1]


def test_colab_exec_that_returns_early_fails_the_batch_and_stops(ll, monkeypatch, tmp_path):
    calls = []

    def fake_colab(*args, timeout=0):
        calls.append(args)
        if args[0] == "download":
            raise RuntimeError("404")  # nothing was written on the VM
        return ""

    monkeypatch.setattr(ll, "colab", fake_colab)
    monkeypatch.setattr(ll, "wslpath", lambda p: str(p))
    monkeypatch.setattr(ll, "stop_orphan_sessions", lambda keep=None: [])
    monkeypatch.setattr(ll, "colab_units", lambda: 0.0)
    monkeypatch.setattr(ll, "colab_rate", lambda: 1.07)
    monkeypatch.setattr(ll, "colab_new_session", lambda s: "T4")
    monkeypatch.setattr(ll, "colab_upload", lambda *a: None)
    audio = tmp_path / "a.flac"
    audio.write_bytes(b"x")
    with ll.db() as con:
        bid = con.execute("INSERT INTO transcribe_batches(mode, lecture_ids, audio_sec) VALUES ('colab','[1]',60)").lastrowid
    lec = {"id": 1, "audio_path": str(audio), "course_id": None, "title": "t"}
    monkeypatch.setattr(ll, "initial_prompt", lambda l: "")
    with pytest.raises(RuntimeError, match="0 of 1 transcript"):
        ll.transcribe_batch_colab([lec], bid)
    execs = [a for a in calls if a[0] == "exec"]
    assert execs and all("--timeout" in a for a in execs)  # the CLI default (30 s) cut batch 2 short
    assert calls[-1][:3] == ("stop", "-s", f"{ll.SESSION_PREFIX}{bid}")


def test_canvas_session_cookies_survive_a_browser_restart(ll):
    class Ctx:
        def __init__(self, jar):
            self.jar = jar

        def cookies(self):
            return list(self.jar)

        def add_cookies(self, cs):
            self.jar += cs

    sess = {"name": "canvas_session", "value": "v1", "domain": "ufl.instructure.com", "path": "/", "expires": -1}
    keep = {"name": "_ga", "value": "g", "domain": ".ufl.edu", "path": "/", "expires": 1900000000}
    cs = ll.CanvasSession()
    cs.ctx = Ctx([sess, keep])
    cs.save_session_cookies()
    assert [c["name"] for c in ll.json.loads(ll.SESSION_COOKIES.read_text())] == ["canvas_session"]
    cs.ctx = Ctx([keep])  # a relaunch: Chromium dropped the session cookie
    assert cs.restore_session_cookies() == 1
    assert sess in cs.ctx.jar
    fresher = dict(sess, value="v2")
    cs.ctx = Ctx([fresher, keep])  # the profile already has a newer one: leave it alone
    assert cs.restore_session_cookies() == 0
    assert cs.ctx.jar == [fresher, keep]


def test_drive_folder_in_a_module_becomes_one_item_per_recording(ll, monkeypatch):
    folder = "https://drive.google.com/drive/folders/1FakeCourseFolder0000000000000000?usp=sharing"
    modules = [{"name": "Resources", "items": [
        {"type": "ExternalUrl", "title": "Lecture Slides/Recordings (Drive)", "external_url": folder,
         "html_url": "https://ufl.instructure.com/courses/11/modules/items/5"},
        {"type": "ExternalUrl", "title": "One clip", "external_url": "https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWxYz/view"}]}]

    def fake_get(path, params=None, paginate=True):
        return modules if path.endswith("/modules") else ([] if paginate else None)

    listed = []
    monkeypatch.setattr(ll, "canvas_get", fake_get)
    monkeypatch.setattr(ll, "CanvasSession", lambda *a, **k: __import__("contextlib").nullcontext())
    ll.set_canvas_state("connected", user="Kashi")
    monkeypatch.setattr(ll, "drive_list_folder", lambda fid: listed.append(fid) or [
        {"id": "1FakeRecording00000000000000000-", "path": "M0L1 Course Overview.mkv"},
        {"id": "1FakeSlides000000000000000000000", "path": "M0L1 Course Overview.pdf"}])
    c = ll.find_course("COP4020")
    c["canvas_id"] = 11
    assert ll.canvas_scan_course(c)["new"] == 2
    assert listed == ["1FakeCourseFolder0000000000000000"]
    with ll.db() as con:
        got = {r[0]: r[1:] for r in con.execute("SELECT key, kind, title, url, page_link FROM canvas_items")}
    assert got["drive:1FakeRecording00000000000000000-"] == (
        "drive", "M0L1 Course Overview", "https://drive.google.com/file/d/1FakeRecording00000000000000000-/view",
        "https://ufl.instructure.com/courses/11/modules/items/5")
    assert "drive:1AbCdEfGhIjKlMnOpQrStUvWxYz" in got  # a single shared file link
    assert not any(k.startswith("drive_folder:") for k in got)  # the folder itself isn't an item
    assert ll.classify_url(got["drive:1FakeRecording00000000000000000-"][2]) == "drive"


def test_unlistable_drive_folder_is_kept_as_one_item(ll, monkeypatch):
    items = {"drive_folder:X": {"key": "drive_folder:X", "kind": "drive_folder", "url": "u", "page_url": "p"}}
    monkeypatch.setattr(ll, "drive_list_folder", lambda fid: (_ for _ in ()).throw(RuntimeError("private")))
    ll.expand_drive_folders(items)
    assert list(items) == ["drive_folder:X"]


def test_drive_file_id_from_share_link_shapes(ll):
    for url in ["https://drive.google.com/file/d/1FakeRecording00000000000000000-/view?usp=sharing",
                "https://drive.google.com/file/u/0/d/1FakeRecording00000000000000000-/view",
                "https://drive.google.com/open?id=1FakeRecording00000000000000000-",
                "https://drive.google.com/uc?export=download&id=1FakeRecording00000000000000000-"]:
        assert ll._DRIVE_FILE_ID.search(url)[1] == "1FakeRecording00000000000000000-", url


# ---- Stage 2 review fixes

def _lecture(ll, status, audio=True):
    with ll.db() as con:
        lid = con.execute("INSERT INTO lectures(title, status, duration_sec) VALUES ('t', ?, 60)", (status,)).lastrowid
        if audio:
            f = ll.DATA / "media" / str(lid) / "audio.flac"
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
            con.execute("UPDATE lectures SET audio_path=? WHERE id=?", (f"media/{lid}/audio.flac", lid))
    return lid


def _fake_local(ll, monkeypatch, fail=False):
    def fake(lec, progress=None, model_name=None):
        if fail:
            raise RuntimeError("cpu blew up")
        return {"model": "base", "segments": [{"start": 0, "end": 1, "text": "hi", "words": []}]}
    monkeypatch.setattr(ll, "transcribe_local", fake)


def test_transcribe_all_waiting_skips_failed_lectures(ll, monkeypatch):
    _fake_local(ll, monkeypatch)
    queued, failed = _lecture(ll, "queued_transcribe"), _lecture(ll, "error")
    out = ll.job_transcribe({"id": 1, "payload": {"mode": "local"}})
    assert out["transcribed"] == [queued]
    assert ll.get_lecture(failed)["status"] == "error"
    assert ll.job_transcribe({"id": 2, "payload": {"mode": "local", "lecture_ids": []}})["transcribed"] == []
    assert ll.job_transcribe({"id": 3, "payload": {"mode": "local", "lecture_ids": [failed]}})["transcribed"] == [failed]


def test_missing_audio_is_marked_and_never_sent_to_colab(ll, monkeypatch):
    monkeypatch.setattr(ll, "transcribe_batch_colab", lambda *a, **k: pytest.fail("nothing should go to Colab"))
    lid = _lecture(ll, "queued_transcribe")
    (ll.DATA / "media" / str(lid) / "audio.flac").unlink()
    assert ll.job_transcribe({"id": 1, "payload": {"mode": "colab"}})["transcribed"] == []
    assert "audio file missing" in ll.get_lecture(lid)["error"]
    assert ll.meta_get("colab_failures", 0) == 0  # a local problem doesn't count against Colab


def test_colab_backoff_expires(ll):
    ll.meta_set("colab_failures", 2)
    ll.meta_set("colab_failed_at", ll.time.time())
    assert ll.colab_backed_off()
    ll.meta_set("colab_failed_at", ll.time.time() - (ll.COLAB_BACKOFF_H * 3600 + 1))
    assert not ll.colab_backed_off()  # auto mode tries Colab again


def test_failed_local_batch_is_closed_and_stale_rows_closed_at_startup(ll, monkeypatch):
    _fake_local(ll, monkeypatch, fail=True)
    _lecture(ll, "queued_transcribe")
    with pytest.raises(RuntimeError, match="cpu blew up"):
        ll.job_transcribe({"id": 1, "payload": {"mode": "local"}})
    with ll.db() as con:
        assert con.execute("SELECT COUNT(*) FROM transcribe_batches WHERE ended_at IS NULL").fetchone()[0] == 0
        con.execute("INSERT INTO transcribe_batches(mode, lecture_ids) VALUES ('colab', '[]')")  # a killed process
    ll.reset_stuck_transcribing()
    with ll.db() as con:
        assert con.execute("SELECT COUNT(*) FROM transcribe_batches WHERE ended_at IS NULL").fetchone()[0] == 0


def test_run_errors_name_the_wsl_tool_and_redact_tokens(ll, monkeypatch):
    class R:
        returncode, stderr = 1, ""
        stdout = 'Error: https://x/proxy?token=abc123&authuser=0 {"token": "sekrit"}'
    monkeypatch.setattr(ll.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError) as e:
        ll.run(["wsl.exe", "-d", "Ubuntu", "-e", "/home/u/.local/bin/colab", "usage"])
    msg = str(e.value)
    assert msg.startswith("colab failed (1)"), msg
    assert "abc123" not in msg and "sekrit" not in msg


def test_colab_usage_parsing_uses_the_real_format(ll):
    real = "Current balance: 0.00 compute units\nUsage rate: 1.07/hr\nActive assignments: 1\n"
    assert ll.parse_colab_units(real) == 0.0
    assert ll.parse_colab_rate(real) == 1.07
    assert ll.parse_colab_units("Current balance: 1,234.50 compute units\n") == 1234.5
    for junk in ["", "Usage rate: 1.76 units/hr", "Error: not logged in", "available: 3 sessions"]:
        assert ll.parse_colab_units(junk) is None, junk


def test_unreadable_balance_fails_closed_unless_colab_was_asked_for(ll, monkeypatch):
    monkeypatch.setattr(ll, "stop_orphan_sessions", lambda keep=None: [])
    monkeypatch.setattr(ll, "colab_units", lambda: None)
    with pytest.raises(ll.LowUnits):
        ll.transcribe_batch_colab([], 1)
    monkeypatch.setattr(ll, "colab_new_session", lambda s: (_ for _ in ()).throw(RuntimeError("got past the guard")))
    monkeypatch.setattr(ll, "colab", lambda *a, **k: "")
    with pytest.raises(RuntimeError, match="got past the guard"):
        ll.transcribe_batch_colab([], 1, force=True)


def test_colab_stop_is_retried_once(ll, monkeypatch):
    stops = []

    def fake_colab(*args, timeout=0):
        if args[0] == "stop":
            stops.append(args)
            if len(stops) == 1:
                raise RuntimeError("wsl hiccup")
        return ""
    monkeypatch.setattr(ll, "colab", fake_colab)
    monkeypatch.setattr(ll, "stop_orphan_sessions", lambda keep=None: [])
    monkeypatch.setattr(ll, "colab_units", lambda: 0.0)
    monkeypatch.setattr(ll, "colab_new_session", lambda s: (_ for _ in ()).throw(RuntimeError("no GPU")))
    with pytest.raises(RuntimeError, match="no GPU"):
        ll.transcribe_batch_colab([], 7)
    assert len(stops) == 2


def test_canvas_outage_is_not_an_expired_session(ll, monkeypatch):
    monkeypatch.setattr(ll, "notify", lambda t, b: pytest.fail("no sign-in toast for an outage"))
    ll.set_canvas_state("connected", user="Kashi")
    for resp in (FakeResponse(502, "<html>Bad gateway</html>", ctype="text/html"),
                 FakeResponse(503, {"errors": "maintenance"}),
                 FakeResponse(200, "<html>Canvas is down for maintenance</html>", ctype="text/html")):
        cs, _ = fake_session(ll, [resp])
        with pytest.raises(ll.CanvasError) as e:
            cs.get("courses")
        assert not isinstance(e.value, ll.CanvasNeedsSignIn)
    assert ll.canvas_state()["state"] == "connected"


def test_canvas_get_never_opens_a_browser_before_you_connect(ll, monkeypatch):
    monkeypatch.setattr(ll, "CanvasSession", lambda *a, **k: pytest.fail("opened the browser"))
    monkeypatch.setattr(ll, "notify", lambda t, b: pytest.fail("no toast"))
    with pytest.raises(ll.CanvasError, match="isn't connected"):
        ll.canvas_get("courses")
    assert ll.canvas_state()["state"] == "not_connected"


def test_canvas_session_cleans_up_when_launch_half_fails(ll, monkeypatch):
    closed = []

    class Ctx:
        def cookies(self):
            return []

        def close(self):
            closed.append("ctx")

    class PW:
        def __init__(self):
            self.chromium = self

        def launch_persistent_context(self, *a, **k):
            return Ctx()

        def start(self):
            return self

        def stop(self):
            closed.append("pw")

    import playwright.sync_api as psa
    monkeypatch.setattr(psa, "sync_playwright", lambda: PW())
    monkeypatch.setattr(ll, "_prepare_profile", lambda: None)
    monkeypatch.setattr(ll.CanvasSession, "restore_session_cookies",
                        lambda self: (_ for _ in ()).throw(ValueError("bad cookie")))
    with pytest.raises(ValueError):
        with ll.CanvasSession():
            pass
    assert closed == ["ctx", "pw"]
    lock = ll.FileLock(ll.DATA / "browser.lock")
    assert lock.acquire()  # the lock was freed too
    lock.release()


def test_login_cookies_only_for_login_hosts(ll):
    yes = ["https://ufl.hosted.panopto.com/Panopto/Pages/Viewer.aspx?id=1", "https://cdnapisec.kaltura.com/p/1/x",
           "https://ufl.instructure.com/courses/1", "https://mediasite.example.edu/Mediasite/Play/abc"]
    no = ["https://evil.example/?r=ufl.edu", "https://notufl.edu.example.com/x", "https://www.youtube.com/watch?v=x"]
    assert all(ll.needs_login_cookies(u) for u in yes)
    assert not any(ll.needs_login_cookies(u) for u in no)


def test_canvas_pull_of_a_public_item_needs_no_browser_and_leaves_no_cookies(ll, monkeypatch, noise):
    monkeypatch.setattr(ll, "CanvasSession", lambda *a, **k: pytest.fail("YouTube/Drive/Zoom need no browser"))
    c = ll.find_course("COP4020")
    with ll.db() as con:
        con.execute("INSERT INTO canvas_items(key, course_id, kind, title, url) VALUES "
                    "('drive:abc', ?, 'drive', 'M0L1', 'https://drive.google.com/file/d/abc/view')", (c["id"],))

    def fake_download(url, dest, **kw):
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "clip.wav"
        f.write_bytes(noise.read_bytes())
        return [(f, {"title": "clip"})]
    monkeypatch.setattr(ll, "download_url", fake_download)
    out = ll.job_canvas_pull({"id": 5, "payload": {"course_id": c["id"], "key": "drive:abc"}})
    assert out["lectures"][0]["lecture_id"]
    assert not ll.job_cookie_file(5).exists()


def test_scan_never_opens_a_browser_when_signed_out(ll, monkeypatch):
    monkeypatch.setattr(ll, "CanvasSession", lambda *a, **k: pytest.fail("opened the browser"))
    monkeypatch.setattr(ll, "notify", lambda t, b: None)
    c = ll.find_course("COP4020")
    c["canvas_id"] = 11
    for state, err in (("not_connected", ll.CanvasError), ("needs_signin", ll.CanvasNeedsSignIn)):
        ll.set_canvas_state(state)
        with pytest.raises(err):
            ll.canvas_scan_course(c)


def test_run_error_keeps_stderr_when_stdout_is_binary(ll, monkeypatch):
    class R:
        returncode, stdout, stderr = 1, b"\x00" * 10_000_000, b"Invalid data found when processing input"
    monkeypatch.setattr(ll.subprocess, "run", lambda *a, **k: R())
    with pytest.raises(RuntimeError) as e:
        ll.run(["ffmpeg", "-i", "x", "-f", "f32le", "-"])
    assert str(e.value) == "ffmpeg failed (1): Invalid data found when processing input"
    class T:
        returncode, stdout, stderr = 2, "", "exit code: 2, see https://x/?code=abc&state=xyz"
    monkeypatch.setattr(ll.subprocess, "run", lambda *a, **k: T())
    with pytest.raises(RuntimeError) as e:
        ll.run(["tool"])
    assert "exit code: 2" in str(e.value) and "abc" not in str(e.value) and "xyz" not in str(e.value)


def test_canvas_pull_jobs_are_labelled_with_the_recording_title(ll):
    from fastapi.testclient import TestClient
    c = ll.find_course("COP4020")
    with ll.db() as con:
        con.execute("INSERT INTO canvas_items(key, course_id, kind, title, url) VALUES "
                    "('drive:abc', ?, 'drive', 'M2L1 Lexer Introduction', 'u')", (c["id"],))
    ll.canvas_pull(c["id"], ["drive:abc"])
    client = TestClient(ll.create_app(), base_url=f"http://127.0.0.1:{ll.PORT}")
    jobs = client.get("/api/jobs").json()
    assert [j["label"] for j in jobs if j["kind"] == "canvas_pull"] == ["M2L1 Lexer Introduction"]


# ---------------------------------------------------------------- slides (Stage 3)
SLIDE_TEXT = [
    "Course overview: grading policy, office hours, exams and the syllabus schedule",
    "Lexical analysis: a lexer turns characters into tokens using regular expressions",
    "Finite automata: a DFA has states, an alphabet, transitions and accepting states",
    "Parsing: a recursive descent parser builds a syntax tree from the token stream",
    "Type checking: the checker walks the syntax tree and rejects ill typed programs",
    "Interpreters: evaluate the syntax tree directly with an environment of bindings",
    "Garbage collection: mark and sweep frees heap objects that are unreachable",
]


def _pdf(path, pages, sizes=None):
    import pymupdf as fitz
    doc = fitz.open()
    for i, text in enumerate(pages):
        w, h = (sizes or {}).get(i, (960, 540))
        p = doc.new_page(width=w, height=h)
        p.insert_text((40, 80), text.split(":")[0], fontsize=32)
        p.insert_textbox(fitz.Rect(40, 120, w - 40, h - 40), text, fontsize=16)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def _deck(ll, title, course_id, path=None, source="drive", status="new", created_at=None):
    with ll.db() as con:
        return con.execute("INSERT INTO decks(course_id, key, source, title, path, status, created_at) "
                           "VALUES (?,?,?,?,?,?,?)", (course_id, f"{source}:{title}", source, title, path, status,
                                                      created_at)).lastrowid


def _ready_deck(ll, title, course_id, pages=SLIDE_TEXT):
    rel = f"Slides/test/{title}.pdf"
    _pdf(ll.DATA / rel, pages)
    did = _deck(ll, title, course_id, path=rel)
    ll.extract_deck(did)
    return did


def _lec(ll, title, course_id, segs=(), video=None, date=None):
    with ll.db() as con:
        lid = con.execute("INSERT INTO lectures(title, course_id, date, status, duration_sec, video_path) "
                          "VALUES (?,?,?,'transcribed',600,?)", (title, course_id, date, video)).lastrowid
        con.executemany("INSERT INTO segments(lecture_id, idx, start, end, text) VALUES (?,?,?,?,?)",
                        [(lid, i, s, e, t) for i, (s, e, t) in enumerate(segs)])
    return lid


def _said(slide_order, seg_sec=15):
    """Two 15 s segments per 30 s window, each window talking about one slide."""
    segs = []
    for w, s in enumerate(slide_order):
        body = SLIDE_TEXT[s].split(":")[1] if s is not None else "okay so yeah let me just grab some water"
        for half in range(2):
            t = (2 * w + half) * seg_sec
            segs.append((t, t + seg_sec, f"so um{body} right"))
    return segs


def test_match_score_reads_codes_titles_and_dates(ll):
    s = lambda d, l, **kw: ll.match_score({"title": d, **kw}, {"title": l, "date": kw.get("date")})  # noqa: E731
    assert s("M2L1 Lexer Introduction", "M2L1 Lexer Introduction") >= 1.0
    assert s("L07 slides", "Lecture 7: Parsing") >= ll.MATCH_MIN          # same code, nothing else
    assert s("Regular Languages", "M2L2 Regular Languages") >= ll.MATCH_MIN  # same title, one side has a code
    assert s("Lecture 8 Parsing", "Lecture 7: Parsing") < 0                # codes contradict
    assert s("Syllabus", "M2L1 Lexer Introduction") < ll.MATCH_MIN
    assert s("M2TL1 Error Handling", "M2TL1 Error Handling") >= 1.0
    assert s("M2L1 Error Handling", "M2TL1 Error Handling") < 0  # TL is a different lecture from L
    assert s("M3TL2-3 Syntax Review", "M3TL3 Syntax Review (cont)") >= ll.MATCH_MIN  # a range covers it
    near =ll.match_score({"title": "Week notes", "created_at": "2026-09-01T10:00:00Z"},
                          {"title": "Notes", "date": "2026-09-02"})
    far = ll.match_score({"title": "Week notes", "created_at": "2026-06-01T10:00:00Z"},
                         {"title": "Notes", "date": "2026-09-02"})
    assert near > far


def test_match_decks_is_one_to_one_skips_ties_and_respects_your_pick(ll):
    cid = ll.find_course("COP4020")["id"]
    a, b = _lec(ll, "M0L1 Course Overview", cid), _lec(ll, "M2L1 Lexer Introduction", cid)
    c, d = _lec(ll, "Review session", cid), _lec(ll, "Review session", cid)
    d1, d2 = _deck(ll, "M0L1 Course Overview", cid), _deck(ll, "M2L1 Lexer Introduction", cid)
    d3 = _deck(ll, "Review session", cid)
    assert sorted(ll.match_decks(cid)) == [a, b]
    deck_of = lambda lid: ll.get_lecture(lid)["deck_id"]  # noqa: E731
    assert (deck_of(a), deck_of(b), deck_of(c), deck_of(d)) == (d1, d2, None, None)  # c/d tie: you pick
    assert ll.match_decks(cid) == []  # stable
    ll.set_lecture_deck(b, d1)  # you say deck 1 belongs to lecture b
    assert ll.get_lecture(b)["deck_how"] == "manual"
    assert deck_of(a) is None  # deck 1 is no longer auto-assigned to a
    ll.match_decks(cid)
    assert (deck_of(a), deck_of(b)) == (None, d1)
    ll.set_lecture_deck(c, d3)
    assert deck_of(d) is None and ll.lecture_slides(c)["deck"]["how"] == "manual"
    # one deck for two lectures when its name says so, but still only one per code
    e, f = _lec(ll, "M3TL2 Syntax Review", cid), _lec(ll, "M3TL3 Syntax Review (cont)", cid)
    g = _lec(ll, "M3TL4 Syntax Review again", cid)
    both = _deck(ll, "M3TL2-3 Syntax Review", cid)
    ll.match_decks(cid)
    assert (deck_of(e), deck_of(f), deck_of(g)) == (both, both, None)


def test_align_follows_skips_one_step_back_and_quiet_stretches(ll):
    truth = [0, 0, 1, 1, 2, 2, 3, 2, 3, 4, 4, 6, 6]  # skips slide 5, goes back from 3 to 2 once
    wins = [SLIDE_TEXT[s].split(":")[1] + " um so basically" for s in truth]
    wins.insert(5, "okay so yeah let me just grab some water")  # nothing slide-related said
    expect = truth[:5] + [truth[4]] + truth[5:]
    assert ll.align_windows(ll.tfidf_sim(wins, SLIDE_TEXT)) == expect


def test_align_gives_up_when_no_slide_text_matches(ll):
    import numpy as np
    assert ll.align_windows(np.zeros((4, 3))) is None
    cid = ll.find_course("COP4020")["id"]
    did = _ready_deck(ll, "Unrelated", cid, pages=["Medieval poetry: sonnets, ballads and heroic couplets"] * 2)
    lid = _lec(ll, "Lexers", cid, segs=_said([1, 2, 3]))
    with ll.db() as con:
        con.execute("UPDATE lectures SET deck_id=?, deck_how='manual' WHERE id=?", (did, lid))
    with pytest.raises(ll.DeckError, match="right deck"):
        ll.align_lecture(lid)


def test_pdf_pages_get_text_titles_and_same_size_renders(ll):
    import pymupdf as fitz
    cid = ll.find_course("COP4020")["id"]
    rel = "Slides/COP4020/mixed.pdf"
    _pdf(ll.DATA / rel, SLIDE_TEXT[:3], sizes={1: (540, 960)})  # page 2 is portrait
    did = _deck(ll, "mixed", cid, path=rel)
    assert ll.extract_deck(did) == {"pages": 3}
    deck = ll.get_deck(did)
    assert (deck["status"], deck["pages"], deck["thumb_w"], deck["thumb_h"]) == ("ready", 3, 320, 180)
    with ll.db() as con:
        titles = [r[0] for r in con.execute("SELECT title FROM slides WHERE deck_id=? ORDER BY no", (did,))]
    assert titles == ["Course overview", "Lexical analysis", "Finite automata"]
    for no in (1, 2, 3):
        for w, h in ((320, 180), (1280, 720)):
            pix = fitz.Pixmap(str(ll.SLIDE_IMAGES / str(did) / f"{no}-{w}.png"))
            assert (pix.width, pix.height) == (w, h), (no, w)


def _pptx(path, slides):
    from pptx import Presentation
    prs = Presentation()
    for title, body in slides:
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = title
        s.placeholders[1].text = body
    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(path)
    return path


def test_pptx_is_converted_with_libreoffice(ll):
    cid = ll.find_course("COP4020")["id"]
    _pptx(ll.DATA / "Slides/COP4020/deck.pptx", [("Lexers", "Tokens come from regular expressions"),
                                                 ("Parsers", "Recursive descent builds the tree")])
    did = _deck(ll, "deck", cid, path="Slides/COP4020/deck.pptx")
    assert ll.extract_deck(did) == {"pages": 2}
    assert ll.get_deck(did)["thumb_w"] == 320
    assert (ll.SLIDE_IMAGES / str(did) / "2-1280.png").is_file()


def test_pptx_falls_back_to_text_and_ppt_reports_how_to_fix(ll, monkeypatch):
    cid = ll.find_course("COP4020")["id"]
    monkeypatch.setattr(ll, "pptx_to_pdf", lambda src, out: (_ for _ in ()).throw(RuntimeError("soffice died")))
    _pptx(ll.DATA / "Slides/COP4020/deck.pptx", [("Lexers", "Tokens come from regular expressions")])
    did = _deck(ll, "deck", cid, path="Slides/COP4020/deck.pptx")
    ll.extract_deck(did)
    assert ll.get_deck(did)["thumb_w"] is None
    with ll.db() as con:
        assert con.execute("SELECT title, text FROM slides WHERE deck_id=?", (did,)).fetchone()[:] == (
            "Lexers", "Lexers\nTokens come from regular expressions")
    (ll.DATA / "Slides/COP4020/old.ppt").write_bytes(b"x")
    old = _deck(ll, "old", cid, path="Slides/COP4020/old.ppt")
    with pytest.raises(ll.DeckError, match="Save it as PDF"):
        ll.extract_deck(old)


def test_find_screens_drops_transients_and_folds_revisits(ll):
    import numpy as np
    a = np.full((90, 160), 250, np.uint8)
    b, c = a.copy(), a.copy()
    b[10:20] = 0                   # a different screen
    c[40:42, 20:120] = 30          # one new line of text: 1.4% of the picture
    frames = np.stack([a, a, a, b, c, c, c, a, a])
    frames[:, 70:90, 130:160] = np.random.default_rng(1).integers(0, 255, (9, 20, 30))  # a webcam inset
    ts = [0, 4, 8, 12, 16, 20, 24, 28, 32]
    screens = ll.find_screens(frames, ts, end=36)
    assert [s["times"] for s in screens] == [[[0, 12], [28, 36]], [[16, 28]]]  # b was on for 4 s
    assert [s["grab"] for s in screens] == [2, 6]  # the last frame of the first run
    assert ll.find_screens(frames[:1], ts[:1], end=10)[0]["times"] == [[0, 10]]


def test_slides_job_aligns_and_the_api_shows_revisits(ll):
    cid = ll.find_course("COP4020")["id"]
    did = _ready_deck(ll, "M2L1 Lexer Introduction", cid)
    lid = _lec(ll, "M2L1 Lexer Introduction", cid, segs=_said([0, 1, 1, 2, 1, 2, 3]))
    out = ll.job_slides({"id": 0, "payload": {"lecture_id": lid}})
    assert out["segments"] == 14
    with ll.db() as con:
        nos = [r[0] for r in con.execute("SELECT slide_no FROM segments WHERE lecture_id=? ORDER BY idx", (lid,))]
    assert nos == [n + 1 for n in (0, 0, 1, 1, 1, 1, 2, 2, 1, 1, 2, 2, 3, 3)]
    s = ll.lecture_slides(lid)
    assert (s["state"], s["deck"]["id"], s["deck"]["how"], s["deck"]["thumb_h"]) == ("aligned", did, "auto", 180)
    by_no = {x["no"]: x for x in s["slides"]}
    assert by_no[2]["ranges"] == [[30.0, 90.0], [120.0, 150.0]] and by_no[2]["first"] == 30.0
    assert by_no[7]["ranges"] == [] and by_no[7]["first"] is None  # never shown
    assert by_no[1]["thumb"] == f"/media/slides/{did}/1-320.png?v={int(ll.get_deck(did)['extracted_at'])}"
    assert s["candidates"][0]["id"] == did and s["candidates"][0]["pages"] == 7
    assert "Lexical analysis" in ll.lecture_slide_terms(lid)
    assert "Slides: " in ll.initial_prompt(ll.get_lecture(lid))


def test_slides_states_say_what_is_missing(ll):
    cid = ll.find_course("COP4020")["id"]
    state = lambda lid: (lambda s: (s["state"], s["reason"] or s["error"]))(ll.lecture_slides(lid))  # noqa: E731
    bare = _lec(ll, "Office hours", cid)
    assert state(bare)[0] == "none"
    video = _lec(ll, "Talk", cid, video="media/x/original.mp4")
    with ll.db() as con:
        con.execute("DELETE FROM segments WHERE lecture_id=?", (video,))
    assert state(video) == ("waiting", "no transcript yet")
    pending = _lec(ll, "M2L1 Lexer Introduction", cid, segs=_said([1]))
    _deck(ll, "M2L1 Lexer Introduction", cid)  # listed on Canvas, not downloaded yet
    ll.match_decks(cid)
    assert state(pending) == ("waiting", "downloading the slides")
    with ll.db() as con:
        con.execute("UPDATE lectures SET slides_error='The deck has no pages. Attach the deck again.' WHERE id=?",
                    (pending,))
    assert state(pending) == ("error", "The deck has no pages. Attach the deck again.")
    ll.set_lecture_deck(bare, None)
    assert state(bare) == ("none", "You chose no slides for this lecture.")


def test_api_slide_upload_errors_pick_and_images(ll, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    c = TestClient(ll.create_app(), base_url=f"http://127.0.0.1:{ll.PORT}")
    cid = ll.find_course("COP4020")["id"]
    lid = _lec(ll, "Parsing day", cid, segs=_said([3, 3, 4]))
    url = f"/api/lectures/{lid}/slides"
    r = c.post(url, files={"file": ("notes.docx", b"x")})
    assert r.status_code == 400 and "PDF" in r.json()["detail"]
    r = c.post(url, files={"file": ("broken.pdf", b"this is not a pdf")})
    assert r.status_code == 422 and "attach" in r.json()["detail"].lower()
    good = _pdf(tmp_path / "Parsing.pdf", SLIDE_TEXT).read_bytes()
    monkeypatch.setattr(ll, "MAX_DECK_MB", 0)
    assert c.post(url, files={"file": ("big.pdf", good)}).status_code == 413
    monkeypatch.setattr(ll, "MAX_DECK_MB", 200)
    r = c.post(url, files={"file": ("..\\CON.pdf", good)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["deck"]["source"], body["deck"]["how"], body["deck"]["pages"]) == ("upload", "manual", 7)
    assert body["state"] == "waiting"  # queued for alignment
    assert (ll.DATA / "Slides" / "COP4020" / "_CON.pdf").is_file()  # no device name, no path parts
    did = body["deck"]["id"]
    img = c.get(f"/media/slides/{did}/1-320.png")
    assert img.status_code == 200 and img.headers["cache-control"] == "no-cache"
    assert c.get(f"/media/slides/{did}/..%5C..%5Clecturelab.db").status_code == 404
    _deck(ll, "Parsing day", cid, status="ready")  # a better-looking auto match appears
    ll.match_decks(cid)
    assert c.get(url).json()["deck"]["id"] == did  # your pick stays
    with ll.db() as con:
        jobs = con.execute("SELECT COUNT(*) FROM jobs WHERE kind='slides' AND lecture_id=? AND status='queued'",
                           (lid,)).fetchone()[0]
    assert jobs == 1  # queued once, not once per change
    r = c.put(url, json={"deck_id": None})
    assert r.json()["state"] == "none"
    assert c.put(url, json={"deck_id": 999}).status_code == 400


def test_canvas_scan_lists_decks_separately_and_queues_downloads(ll, monkeypatch):
    folder = "https://drive.google.com/drive/folders/1FakeCourseFolder0000000000000000"
    files = {"video": [], "pdf": [{"id": 77, "display_name": "Lecture 7 Parsing.pdf", "content-type": "application/pdf",
                                   "size": 1 << 20, "created_at": "2026-09-01T12:00:00Z"},
                                  {"id": 78, "display_name": "Huge scan.pdf", "content-type": "application/pdf",
                                   "size": 900 << 20}]}

    def fake_get(path, params=None, paginate=True):
        if path.endswith("/files"):
            return files["pdf"] if "application/pdf" in params["content_types[]"] else files["video"]
        if path.endswith("/modules"):
            return [{"name": "Resources", "items": [{"type": "ExternalUrl", "title": "Drive", "external_url": folder}]}]
        return [] if paginate else None

    monkeypatch.setattr(ll, "canvas_get", fake_get)
    monkeypatch.setattr(ll, "CanvasSession", lambda *a, **k: __import__("contextlib").nullcontext())
    monkeypatch.setattr(ll, "drive_list_folder", lambda fid: [
        {"id": "1FakeRecording00000000000000000-", "path": "M0L1 Course Overview.mkv"},
        {"id": "1FakeSlides000000000000000000000", "path": "M0L1 Course Overview.pdf"}])
    ll.set_canvas_state("connected", user="Kashi")
    c = ll.find_course("COP4020")
    c["canvas_id"] = 11
    out = ll.canvas_scan_course(c)
    assert (out["new"], out["decks"], out["decks_new"]) == (1, 2, 2)  # the 900 MB scan is skipped
    with ll.db() as con:
        decks = {r[0]: r[1:] for r in con.execute("SELECT key, source, title, status FROM decks")}
        items = [r[0] for r in con.execute("SELECT key FROM canvas_items")]
        pulls = con.execute("SELECT COUNT(*) FROM jobs WHERE kind='deck_pull'").fetchone()[0]
    assert decks == {"canvas-file:77": ("canvas", "Lecture 7 Parsing", "new"),
                     "drive:1FakeSlides000000000000000000000": ("drive", "M0L1 Course Overview", "new")}
    assert items == ["drive:1FakeRecording00000000000000000-"]  # the Canvas tab only lists recordings
    assert pulls == 2
    assert ll.canvas_scan_course(c)["decks_new"] == 0  # a rescan doesn't queue them again


def test_deck_download_reads_it_and_queues_the_matching_lecture(ll, monkeypatch, tmp_path):
    import gdown
    cid = ll.find_course("COP4020")["id"]
    lid = _lec(ll, "M0L1 Course Overview", cid, segs=_said([0, 0, 1]))
    did = _deck(ll, "M0L1 Course Overview", cid)
    src = _pdf(tmp_path / "src.pdf", SLIDE_TEXT[:3])

    def fake_download(id, output, quiet):
        dest = __import__("pathlib").Path(output) / "M0L1 Course Overview.pdf"
        dest.write_bytes(src.read_bytes())
        return str(dest)

    monkeypatch.setattr(gdown, "download", fake_download)
    with ll.db() as con:
        con.execute("UPDATE decks SET key='drive:1FakeSlides000000000000000000000' WHERE id=?", (did,))
    out = ll.job_deck_pull({"id": 5, "payload": {"deck_id": did}})
    assert out == {"pages": 3, "lectures": [lid]}
    assert ll.get_deck(did)["path"] == "Slides/COP4020/M0L1 Course Overview.pdf"
    ll.job_slides({"id": 0, "payload": {"lecture_id": lid}})
    assert ll.lecture_slides(lid)["state"] == "aligned"


def test_slides_read_off_a_video_with_revisits(ll, tmp_path):
    import pymupdf as fitz
    pdf = fitz.open(_pdf(tmp_path / "s.pdf", SLIDE_TEXT[1:3]))
    imgs = []
    for i, page in enumerate(pdf):
        imgs.append(tmp_path / f"{i}.png")
        ll.render_fixed(page, 1280, 720).save(imgs[-1])
    video = ll.DATA / "media" / "v" / "original.mp4"
    video.parent.mkdir(parents=True)
    seq = [imgs[0], imgs[1], imgs[0]]  # slide A, slide B, back to A: 20 s each
    args = [ll.find_tool("ffmpeg"), "-v", "error"]
    for im in seq:
        args += ["-loop", "1", "-t", "20", "-framerate", "5", "-i", str(im)]
    args += ["-filter_complex", "".join(f"[{i}:v]" for i in range(len(seq))) + f"concat=n={len(seq)}:v=1[v]",
             "-map", "[v]", "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(video)]
    subprocess.run(args, check=True)
    cid = ll.find_course("COP4020")["id"]
    lid = _lec(ll, "Board day", cid, video="media/v/original.mp4",
               segs=[(t, t + 10, "talking") for t in range(0, 60, 10)])
    out = ll.job_slides({"id": 0, "payload": {"lecture_id": lid}})
    s = ll.lecture_slides(lid)
    assert out["pages"] == 2 and s["deck"]["source"] == "ocr" and s["deck"]["thumb_h"] == 180
    assert "lexer" in s["slides"][0]["text"].lower() and "dfa" in s["slides"][1]["text"].lower()
    assert [len(x["ranges"]) for x in s["slides"]] == [2, 1]  # A came back
    with ll.db() as con:
        nos = [r[0] for r in con.execute("SELECT slide_no FROM segments WHERE lecture_id=? ORDER BY idx", (lid,))]
    assert nos == [1, 1, 2, 2, 1, 1]
    did = s["deck"]["id"]
    ll.delete_lecture(lid)
    assert ll.get_deck(did) is None and not (ll.SLIDE_IMAGES / str(did)).exists()


def _slide_video(ll, tmp_path, shots):
    """An h264 video of (png or None for a dark 'IDE' screen, seconds) shots, keyframe every 2 s."""
    args = [ll.find_tool("ffmpeg"), "-v", "error"]
    for img, sec in shots:
        args += (["-loop", "1", "-t", str(sec), "-framerate", "5", "-i", str(img)] if img else
                 ["-f", "lavfi", "-t", str(sec), "-i", "color=c=0x1e1f22:s=1280x720:r=5"])
    video = ll.DATA / "media" / "vv" / "original.mp4"
    video.parent.mkdir(parents=True, exist_ok=True)
    args += ["-filter_complex", "".join(f"[{i}:v]setsar=1[v{i}];" for i in range(len(shots))) +
             "".join(f"[v{i}]" for i in range(len(shots))) + f"concat=n={len(shots)}:v=1[v]",
             "-map", "[v]", "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(video)]
    subprocess.run(args, check=True)
    return "media/vv/original.mp4"


def test_video_that_shows_the_slides_places_them_and_marks_demos(ll, tmp_path):
    import pymupdf as fitz
    cid = ll.find_course("COP4020")["id"]
    did = _ready_deck(ll, "M3L1 Parser Introduction", cid, pages=SLIDE_TEXT[1:4])
    pdf = fitz.open(ll.DATA / ll.get_deck(did)["path"])
    imgs = []
    for i, page in enumerate(pdf):
        imgs.append(tmp_path / f"p{i}.png")
        ll.render_fixed(page, 1280, 720).save(imgs[-1])
    video = _slide_video(ll, tmp_path, [(imgs[0], 20), (None, 40), (imgs[1], 20), (imgs[2], 20)])
    # nothing said matches any slide: only the video can place them
    lid = _lec(ll, "M3L1 Parser Introduction", cid, video=video,
               segs=[(t, t + 10, "okay so yeah") for t in range(0, 100, 10)])
    ll.job_slides({"id": 0, "payload": {"lecture_id": lid}})
    with ll.db() as con:
        nos = [r[0] for r in con.execute("SELECT slide_no FROM segments WHERE lecture_id=? ORDER BY idx", (lid,))]
    assert nos == [1, 1, 1, None, None, None, 2, 2, 2, 3]
    s = ll.lecture_slides(lid)
    assert s["state"] == "aligned" and [x["ranges"] for x in s["slides"]] == [[[0, 30]], [[60, 90]], [[90, 100]]]


def test_deck_download_that_fails_for_a_passing_reason_waits_and_retries(ll, monkeypatch):
    import gdown
    cid = ll.find_course("COP4020")["id"]
    did = _deck(ll, "M0L1 Course Overview", cid)
    with ll.db() as con:
        con.execute("UPDATE decks SET key='drive:1FakeSlides000000000000000000000' WHERE id=?", (did,))

    def boom(**kw):
        raise ConnectionError(rf"{ll.DATA}\downloads\job1 unreachable")

    monkeypatch.setattr(gdown, "download", boom)
    jid = ll.enqueue("deck_pull", {"deck_id": did, "title": "M0L1"})
    for n in range(ll.DECK_MAX_TRIES - 1):  # five tries in all
        with pytest.raises(ll.RetryLater):
            ll.job_deck_pull(ll.get_job(jid))
        j = ll.get_job(jid)
        assert (j["status"], j["payload"]["tries"], ll.get_deck(did)["status"]) == ("queued", n + 1, "new")
        assert j["retry_at"]
    with pytest.raises(ll.DeckError):
        ll.job_deck_pull(ll.get_job(jid))
    deck = ll.get_deck(did)
    assert deck["status"] == "error" and str(ll.DATA) not in deck["error"] and "attach it" in deck["error"]
    assert ll.retry_deck(did) and ll.get_deck(did)["status"] == "new"  # "look for slides" asks again
    # signed out of Canvas: it waits for you, however long, without using up its tries
    cdid = _deck(ll, "M2L1 Lexer Introduction", cid, source="canvas")
    with ll.db() as con:
        con.execute("UPDATE decks SET url='canvas_file:7' WHERE id=?", (cdid,))
    cj = ll.enqueue("deck_pull", {"deck_id": cdid, "title": "M2L1"})
    for _ in range(ll.DECK_MAX_TRIES + 2):
        with pytest.raises(ll.RetryLater):
            ll.job_deck_pull(ll.get_job(cj))
    assert ll.get_job(cj)["payload"]["tries"] == 0 and ll.get_deck(cdid)["status"] == "new"
    assert ll.get_job(cj)["progress"] == "waiting for Canvas sign-in" and ll.get_job(cj)["retry_at"]
    ll.set_canvas_state("connected", user="test")  # you sign in: it goes now, not in half an hour
    assert ll.get_job(cj)["retry_at"] is None


def test_cli_slides_does_not_hang_on_a_download_that_waits(ll, monkeypatch, capsys):
    import gdown
    sys.path.insert(0, str(ll.ROOT))
    import cli
    monkeypatch.setattr(cli, "ll", ll)
    monkeypatch.setattr(gdown, "download", lambda **kw: None)  # Drive returns nothing: a passing failure
    cid = ll.find_course("COP4020")["id"]
    did = _deck(ll, "M0L1 Course Overview", cid)
    jid = ll.enqueue("deck_pull", {"deck_id": did, "title": "M0L1"})
    assert cli.cmd_slides(type("A", (), {"ids": []})()) == 0
    assert "next try after" in capsys.readouterr().out
    assert ll.get_job(jid)["status"] == "queued"


def test_a_second_copy_of_a_deck_keeps_the_match_and_its_alignment(ll):
    cid = ll.find_course("COP4020")["id"]
    lid = _lec(ll, "M2L1 Lexer Introduction", cid, segs=_said([1, 1, 2]))
    pdf = _ready_deck(ll, "M2L1 Lexer Introduction", cid)
    ll.job_slides({"id": 0, "payload": {"lecture_id": lid}})
    assert ll.lecture_slides(lid)["state"] == "aligned"
    _deck(ll, "M2L1 Lexer Introduction", cid, source="canvas")  # the PPTX of it, posted on Canvas too
    assert ll.match_decks(cid) == []
    assert ll.get_lecture(lid)["deck_id"] == pdf and ll.lecture_slides(lid)["state"] == "aligned"
    # a new lecture whose deck was posted twice still gets one: the PDF
    l2 = _lec(ll, "M2L2 Regular Languages", cid)
    _deck(ll, "M2L2 Regular Languages", cid, source="canvas", path="Slides/x/M2L2 Regular Languages.pptx")
    b = _deck(ll, "M2L2 Regular Languages", cid, path="Slides/x/M2L2 Regular Languages.pdf")
    assert ll.match_decks(cid) == [l2] and ll.get_lecture(l2)["deck_id"] == b
    # a range deck you picked for one lecture still serves the other one in its range
    e, f = _lec(ll, "M3TL2 Syntax Review", cid), _lec(ll, "M3TL3 Syntax Review (cont)", cid)
    both = _deck(ll, "M3TL2-3 Syntax Review", cid)
    ll.set_lecture_deck(e, both)
    assert ll.get_lecture(f)["deck_id"] == both


def test_a_pick_made_while_the_video_is_read_wins(ll, monkeypatch):
    cid = ll.find_course("COP4020")["id"]
    lid = _lec(ll, "Guest talk", cid, segs=_said([1, 2]), video="media/x/original.mp4")
    did = _ready_deck(ll, "Some deck", cid)
    other = _deck(ll, "Video frames", cid, source="ocr", status="ready")

    def slow_ocr(lec, progress=None):
        ll.set_lecture_deck(lid, did)  # you attach a deck while the video is being read
        return other

    monkeypatch.setattr(ll, "ocr_video_deck", slow_ocr)
    ll.job_slides({"id": 0, "payload": {"lecture_id": lid}})
    lec = ll.get_lecture(lid)
    assert (lec["deck_id"], lec["deck_how"]) == (did, "manual")


def test_a_new_transcript_is_not_shown_as_aligned(ll):
    cid = ll.find_course("COP4020")["id"]
    lid = _lec(ll, "M2L1 Lexer Introduction", cid, segs=_said([1, 2]))
    _ready_deck(ll, "M2L1 Lexer Introduction", cid)
    ll.job_slides({"id": 0, "payload": {"lecture_id": lid}})
    assert ll.lecture_slides(lid)["state"] == "aligned"
    ll.store_transcript(lid, {"segments": [{"start": 0, "end": 10, "text": "a lexer makes tokens"}]}, "local")
    s = ll.lecture_slides(lid)
    assert s["state"] == "waiting" and all(x["first"] is None for x in s["slides"])


def test_big_and_scanned_pdfs_are_bounded(ll, monkeypatch, tmp_path):
    import pymupdf as fitz
    cid = ll.find_course("COP4020")["id"]
    rel = "Slides/test/book.pdf"
    _pdf(ll.DATA / rel, SLIDE_TEXT[:4])
    monkeypatch.setattr(ll, "MAX_DECK_PAGES", 3)
    with pytest.raises(ll.DeckError, match="4 pages"):
        ll.extract_deck(_deck(ll, "Textbook", cid, path=rel))
    # scans: two near-empty pages, and a page-sized picture with a footer; OCR stops at the cap
    pic = tmp_path / "pic.png"
    ll.render_fixed(fitz.open(_pdf(tmp_path / "p.pdf", SLIDE_TEXT[1:2]))[0], 960, 540).save(pic)
    doc = fitz.open()
    for _ in range(2):
        doc.new_page(width=960, height=540).insert_text((40, 80), "x")
    p = doc.new_page(width=960, height=540)
    p.insert_image(p.rect, filename=str(pic))
    p.insert_text((40, 530), "COP4020 Programming Language Concepts", fontsize=10)
    rel = "Slides/test/scan.pdf"
    doc.save(ll.DATA / rel)
    seen = []
    monkeypatch.setattr(ll, "ocr_image", lambda path: seen.append(path.name) or "read off the picture, lexer tokens")
    monkeypatch.setattr(ll, "OCR_MAX_PAGES", 2)
    did = _deck(ll, "Scanned", cid, path=rel)
    ll.extract_deck(did)
    assert seen == ["1-1280.png", "2-1280.png"]
    monkeypatch.setattr(ll, "OCR_MAX_PAGES", 5)
    seen.clear()
    ll.extract_deck(did)
    assert seen == ["1-1280.png", "2-1280.png", "3-1280.png"]  # the footer alone doesn't count as text
    with ll.db() as con:
        assert con.execute("SELECT text FROM slides WHERE deck_id=? AND no=3", (did,)).fetchone()[0].startswith("read")


def test_slide_errors_shown_to_you_carry_no_local_paths(ll, monkeypatch):
    cid = ll.find_course("COP4020")["id"]
    lid = _lec(ll, "M2L1 Lexer Introduction", cid, segs=_said([1, 2]))
    _ready_deck(ll, "M2L1 Lexer Introduction", cid)

    def broken(lecture_id):
        raise RuntimeError(rf"ffmpeg.exe failed (1): {ll.DATA}\media\5\original.mp4: Invalid data")

    monkeypatch.setattr(ll, "align_lecture", broken)
    with pytest.raises(RuntimeError):
        ll.job_slides({"id": 0, "payload": {"lecture_id": lid}})
    s = ll.lecture_slides(lid)
    assert s["state"] == "error" and str(ll.DATA) not in s["error"] and "server.log" in s["error"]
