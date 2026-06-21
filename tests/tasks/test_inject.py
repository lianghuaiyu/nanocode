from nanocode.tasks.manager import TaskManager
from nanocode.tasks import inject


def test_render_task_reminder(tmp_path):
    m = TaskManager(); t = m.create_task("shell", "pytest -q")
    out = tmp_path / "o.log"; out.write_text("42 passed in 1.2s")
    m.update_task(t.id, status="completed", exit_code=0, stdout_path=str(out), result_summary="42 passed in 1.2s")
    txt = inject.render_task_reminder(m.get_task(t.id))
    assert "<system-reminder>" in txt and "</system-reminder>" in txt
    assert "task-001" in txt and "completed" in txt and "pytest -q" in txt and "42 passed" in txt and str(out) in txt


def test_render_task_reminder_host_job_surfaces_result_path(tmp_path):
    """Host jobs with result_path surface result_path + summary and skip empty stdout."""
    m = TaskManager(); t = m.create_task("memory_consolidate", "investigate parser")
    rp = tmp_path / "result.md"; rp.write_text("full transcript here")
    m.update_task(t.id, status="completed", result_path=str(rp),
                  result_summary="Found 2 bugs in the parser")
    txt = inject.render_task_reminder(m.get_task(t.id))
    assert "<system-reminder>" in txt and "</system-reminder>" in txt
    assert str(rp) in txt
    assert "Found 2 bugs in the parser" in txt
    # no empty stdout Output-tail noise for result-backed host jobs
    assert "Output tail:" not in txt
    assert "(empty)" not in txt


def test_collect_pending_only_terminal_uninjected():
    m = TaskManager(); m.create_task("shell", "a")
    b = m.create_task("shell", "b"); m.update_task(b.id, status="completed")
    c = m.create_task("shell", "c"); m.update_task(c.id, status="failed", injected=True)
    assert [t.id for t in inject.collect_pending_injections(m)] == [b.id]


def test_collect_does_not_mutate_injected():
    m = TaskManager(); t = m.create_task("shell", "x"); m.update_task(t.id, status="completed")
    inject.collect_pending_injections(m)
    assert m.get_task(t.id).injected is False
