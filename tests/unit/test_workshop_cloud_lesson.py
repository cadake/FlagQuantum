from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit
SCRIPT = (
    Path(__file__).resolve().parents[2] / "workshops/flagos2026/scripts/cloud_lesson.py"
)
spec = importlib.util.spec_from_file_location("cloud_lesson", SCRIPT)
assert spec and spec.loader
lessons = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = lessons
spec.loader.exec_module(lessons)


class FakeJob:
    id = "saved-task"
    target = "quafu:Dongling"
    raw_status = "Queued"

    def __init__(self) -> None:
        self.outcome = "pending"

    def save(self, path: Path) -> None:
        with path.open("x") as stream:
            json.dump({"id": self.id}, stream)

    def status(self) -> str:
        return "succeeded" if self.outcome == "completed" else "queued"

    def wait(self, *, timeout: int, poll_interval: int) -> SimpleNamespace:
        assert (timeout, poll_interval) == (30, 3)
        if self.outcome == "pending":
            raise TimeoutError("Queued")
        if self.outcome == "failed":
            raise RuntimeError("Backend failed")
        return SimpleNamespace(counts=[{"00": 512, "11": 512}])


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.setenv("QUAFU_API_KEY", "test-only")
    monkeypatch.setenv("JIUDING_PROJECT", "test.project")
    monkeypatch.setenv("JIUDING_QUEUE", "test-queue")
    (tmp_path / "outputs").mkdir()
    lesson = lessons.CloudLesson(tmp_path, "bell")
    job = FakeJob()
    submissions: list[dict[str, Any]] = []
    fq = ModuleType("flagquantum")

    def submit(circuit: object, **kwargs: Any) -> FakeJob:
        submissions.append(kwargs)
        job.target = kwargs["target"]
        return job

    def restore(path: Path) -> FakeJob:
        assert json.loads(path.read_text())["id"] == job.id
        return job

    fq.__dict__.update(
        submit=submit, restore_job=restore, counts=lambda: "counts-request"
    )
    ir = ModuleType("flagquantum.core.ir")
    ir.__dict__["ensure_circuit_ir"] = lambda circuit: SimpleNamespace(
        content_hash="bell-hash"
    )
    monkeypatch.setitem(sys.modules, "flagquantum", fq)
    monkeypatch.setitem(sys.modules, "flagquantum.core.ir", ir)
    return SimpleNamespace(lesson=lesson, job=job, submissions=submissions, fq=fq)


@pytest.mark.parametrize("target", ("jiuding:cpu", "quafu:Dongling"))
def test_disabled_lesson_never_submits(setup: SimpleNamespace, target: str) -> None:
    assert setup.lesson.submit_once(object(), target=target, enabled=False) is None
    assert setup.submissions == []
    assert not setup.lesson.record.exists()


@pytest.mark.parametrize("target", ("jiuding:cpu", "quafu:Dongling"))
def test_public_api_and_rerun_restore_same_task(
    setup: SimpleNamespace, target: str
) -> None:
    kwargs = (
        {"shots": 1024} if target.startswith("quafu:") else {"image": "verified:image"}
    )
    job = setup.lesson.submit_once(object(), target=target, enabled=True, **kwargs)
    restored = setup.lesson.submit_once(object(), target=target, enabled=True, **kwargs)
    assert restored is job
    assert len(setup.submissions) == 1
    request = setup.submissions[0]
    assert request["target"] == target and request["compiler"] is None
    assert request["queue"] == ("test-queue" if target.startswith("jiuding:") else None)
    assert request["outputs"] == (
        "counts-request" if target.startswith("quafu:") else None
    )
    record = json.loads(setup.lesson.record.read_text())
    assert record["task_id"] == job.id
    assert "test-only" not in setup.lesson.record.read_text()


def test_pending_collection_does_not_resubmit(setup: SimpleNamespace) -> None:
    job = setup.lesson.submit_once(
        object(), target="quafu:Dongling", shots=1024, enabled=True
    )
    assert setup.lesson.collect(job) is None
    assert setup.lesson.collect(job) is None
    assert len(setup.submissions) == 1
    assert setup.lesson.receipt.exists()


def test_completed_counts_keep_notebook06_file_contract(setup: SimpleNamespace) -> None:
    job = setup.lesson.submit_once(
        object(), target="quafu:Dongling", shots=1024, enabled=True
    )
    setup.job.outcome = "completed"
    result = setup.lesson.collect(job)
    record = json.loads(setup.lesson.record.read_text())
    assert record["source"] == "live" and record["status"] == "completed"
    assert record["counts"] == result.counts[0]
    assert record["shots"] == 1024 and record["task_id"] == job.id


def test_failed_job_is_not_replaced_by_local_result(setup: SimpleNamespace) -> None:
    job = setup.lesson.submit_once(object(), target="quafu:Dongling", enabled=True)
    setup.job.outcome = "failed"
    with pytest.raises(RuntimeError, match="Backend failed"):
        setup.lesson.collect(job)
    assert len(setup.submissions) == 1


def test_changed_experiment_is_rejected(setup: SimpleNamespace) -> None:
    setup.lesson.submit_once(
        object(), target="quafu:Dongling", shots=1024, enabled=True
    )
    with pytest.raises(RuntimeError, match="another experiment"):
        setup.lesson.submit_once(
            object(), target="quafu:Dongling", shots=2048, enabled=True
        )
    assert len(setup.submissions) == 1


def test_uncertain_attempt_is_never_retried(setup: SimpleNamespace) -> None:
    setup.lesson.record.write_text('{"status":"submission_started"}')
    with pytest.raises(RuntimeError, match="do not resubmit"):
        setup.lesson.submit_once(object(), target="quafu:Dongling", enabled=True)
    assert setup.submissions == []


def test_save_failure_preserves_printed_task_id(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    def reject_save(path: Path) -> None:
        raise OSError("Storage unavailable")

    monkeypatch.setattr(setup.job, "save", reject_save)
    with pytest.raises(OSError, match="Storage unavailable"):
        setup.lesson.submit_once(object(), target="quafu:Dongling", enabled=True)
    assert json.loads(setup.lesson.record.read_text())["task_id"] == setup.job.id
    with pytest.raises(RuntimeError, match="do not resubmit"):
        setup.lesson.submit_once(object(), target="quafu:Dongling", enabled=True)
    assert len(setup.submissions) == 1


def test_prepare_lesson_finds_deployed_tree_without_cloud_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("JIUDING_AK", raising=False)
    exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: False if p == Path("/etc/accesskey") else exists(p)
    )
    workshop = tmp_path / "flagos2026"
    start = workshop / "notebooks/cloud"
    start.mkdir(parents=True)
    lesson = lessons.prepare_lesson(start, "bell")
    assert lesson.workshop == workshop
    assert lesson.outputs.is_dir()
    with pytest.raises(ValueError, match="safe filename"):
        lessons.prepare_lesson(start, "../other-user")


@pytest.mark.parametrize(
    "filename", ("04_jiuding_jobs.ipynb", "05_quafu_hardware.ipynb")
)
@pytest.mark.parametrize("enabled", (False, True))
def test_each_teaching_cell_runs_in_order(
    setup: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    enabled: bool,
) -> None:
    class State:
        def abs(self) -> State:
            return self

        def square(self) -> State:
            return self

        def tolist(self) -> list[float]:
            return [0.5, 0, 0, 0.5]

    class Circuit:
        def __init__(self, n: int) -> None:
            assert n == 2

        def h(self, wire: int) -> Circuit:
            return self

        def cx(self, control: int, target: int) -> Circuit:
            return self

    start = tmp_path / "flagos2026/notebooks/cloud"
    start.mkdir(parents=True)
    monkeypatch.chdir(start)
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    monkeypatch.delenv("JIUDING_AK", raising=False)
    monkeypatch.setenv("JIUDING_IMAGE", "verified:image")
    exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: False if p == Path("/etc/accesskey") else exists(p)
    )
    setup.fq.__dict__.update(
        Circuit=Circuit, run=lambda circuit: SimpleNamespace(state=State())
    )
    setup.job.outcome = "completed"

    def wait(**kwargs: int) -> SimpleNamespace:
        return SimpleNamespace(state=State(), counts=[{"00": 512, "11": 512}])

    monkeypatch.setattr(setup.job, "wait", wait)
    torch = ModuleType("torch")

    def assert_close(a: State, b: State) -> None:
        assert a.tolist() == b.tolist()

    torch.__dict__["testing"] = SimpleNamespace(assert_close=assert_close)
    monkeypatch.setitem(sys.modules, "torch", torch)
    notebook = json.loads(
        (SCRIPT.parent.parent / "notebooks/cloud" / filename).read_text()
    )
    scope: dict[str, Any] = {}
    for _ in range(2):
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                source = "".join(cell["source"])
                if enabled:
                    source = source.replace(
                        "submit_to_cloud = False", "submit_to_cloud = True"
                    )
                    source = source.replace(
                        "submit_to_hardware = False", "submit_to_hardware = True"
                    )
                exec(source, scope)
    assert len(setup.submissions) == int(enabled)
