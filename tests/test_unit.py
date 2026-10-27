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
