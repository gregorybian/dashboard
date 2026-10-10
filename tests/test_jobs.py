
from app import gtasks, jobs


def test_edit_during_sync_triggers_another_pass(monkeypatch):
    runs = []

    def fake_sync():
        runs.append(1)
        if len(runs) == 1:
            jobs.tasks_changed()  # an edit lands while the first pass is running
        return False
    monkeypatch.setattr(gtasks, "sync", fake_sync)
    jobs.sync_tasks()
    assert len(runs) == 2


def test_no_extra_pass_without_edits(monkeypatch):
    runs = []
    monkeypatch.setattr(gtasks, "sync", lambda: runs.append(1))
    jobs.sync_tasks()
    assert len(runs) == 1
