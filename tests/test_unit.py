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
