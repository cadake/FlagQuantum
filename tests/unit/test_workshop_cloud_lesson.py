from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.unit
WORKSHOP = Path(__file__).resolve().parents[2] / "workshops/flagos2026"


class State:
    def abs(self) -> State:
        return self

    def square(self) -> State:
        return self

    def tolist(self) -> list[float]:
        return [0.5, 0, 0, 0.5]


class Circuit:
    def __init__(self, qubits: int) -> None:
        assert qubits == 2

    def h(self, wire: int) -> Circuit:
        return self

    def cx(self, control: int, target: int) -> Circuit:
        return self

    def draw(self, *, format: str, show_all_qubits: bool) -> tuple[None, None]:
        assert format == "mpl" and show_all_qubits
        return None, None


class FakeJob:
    id = "saved-task"
    raw_status = "Queued"

    def __init__(self) -> None:
        self.outcome = "completed"

    def status(self) -> str:
        return "succeeded" if self.outcome == "completed" else "queued"

    def save(self, path: Path) -> None:
        with path.open("x") as stream:
            json.dump({"id": self.id}, stream)

    def wait(self, *, timeout: int) -> SimpleNamespace:
        assert timeout == 30
        if self.outcome == "pending":
            raise TimeoutError("Queued")
        if self.outcome == "failed":
            raise RuntimeError("Backend failed")
        return SimpleNamespace(state=State(), counts=[{"00": 512, "11": 512}])


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    start = tmp_path / "flagos2026/notebooks"
    start.mkdir(parents=True)
    monkeypatch.chdir(start)
    monkeypatch.syspath_prepend(str(WORKSHOP / "scripts"))
    monkeypatch.delenv("JIUDING_AK", raising=False)
    monkeypatch.delenv("QUAFU_TARGET", raising=False)
    exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda p: False if p == Path("/etc/accesskey") else exists(p)
    )
    for key, value in {
        "QUAFU_API_KEY": "test-only",
        "JIUDING_PROJECT": "test.project",
        "JIUDING_IMAGE": "verified:image",
        "JIUDING_QUEUE": "test-queue",
    }.items():
        monkeypatch.setenv(key, value)
    job = FakeJob()
    calls: list[dict[str, Any]] = []

    def submit(circuit: object, **kwargs: Any) -> FakeJob:
        calls.append(kwargs)
        return job

    def restore(path: Path) -> FakeJob:
        assert json.loads(path.read_text())["id"] == job.id
        return job

    fq = ModuleType("flagquantum")
    fq.__dict__.update(
        Circuit=Circuit,
        run=lambda circuit: SimpleNamespace(state=State()),
        submit=submit,
        restore_job=restore,
        counts=lambda: "counts-request",
    )
    ir = ModuleType("flagquantum.core.ir")
    ir.__dict__["ensure_circuit_ir"] = lambda circuit: SimpleNamespace(
        content_hash="bell-hash"
    )
    monkeypatch.setitem(sys.modules, "flagquantum", fq)
    monkeypatch.setitem(sys.modules, "flagquantum.core.ir", ir)
    torch = ModuleType("torch")

    def assert_close(a: State, b: State) -> None:
        assert a.tolist() == b.tolist()

    torch.__dict__["testing"] = SimpleNamespace(assert_close=assert_close)
    monkeypatch.setitem(sys.modules, "torch", torch)
    matplotlib = ModuleType("matplotlib")
    pyplot = ModuleType("matplotlib.pyplot")
    pyplot.__dict__["show"] = lambda: None
    matplotlib.__dict__["pyplot"] = pyplot
    monkeypatch.setitem(sys.modules, "matplotlib", matplotlib)
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", pyplot)
    return SimpleNamespace(job=job, calls=calls, outputs=start.parent / "outputs")


def run_cells(filename: str, *, enabled: bool) -> dict[str, Any]:
    notebook = json.loads((WORKSHOP / "notebooks" / filename).read_text())
    scope: dict[str, Any] = {}
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
            # Rendering is checked in real kernels by verify_notebooks.py;
            # this provider-free harness executes the submission/recovery cells.
            source = source.replace("%matplotlib inline", "")
            exec(source, scope)
    return scope


@pytest.mark.parametrize(
    "filename", ("05_jiuding_jobs.ipynb", "06_quafu_hardware.ipynb")
)
@pytest.mark.parametrize("enabled", (False, True))
def test_all_standard_api_cells_run_and_reruns_do_not_submit(
    setup: SimpleNamespace, filename: str, enabled: bool
) -> None:
    for _ in range(2):
        scope = run_cells(filename, enabled=enabled)
    assert len(setup.calls) == int(enabled)
    if enabled:
        assert scope["job"] is setup.job
        assert scope["receipt_path"].exists()
        request = setup.calls[0]
        if filename.startswith("05"):
            assert (
                request["queue"] == "test-queue"
                and request["image"] == "verified:image"
            )
        else:
            assert (
                request["outputs"] == "counts-request" and request["compiler"] is None
            )
            record = json.loads((setup.outputs / "quafu-bell.json").read_text())
            assert (
                record["counts"] == {"00": 512, "11": 512}
                and record["source"] == "live"
            )


@pytest.mark.parametrize(
    "filename", ("05_jiuding_jobs.ipynb", "06_quafu_hardware.ipynb")
)
def test_pending_result_is_not_failure_or_duplicate(
    setup: SimpleNamespace, filename: str
) -> None:
    setup.job.outcome = "pending"
    assert run_cells(filename, enabled=True)["result"] is None
    assert run_cells(filename, enabled=True)["result"] is None
    assert len(setup.calls) == 1


def test_changed_request_is_rejected(setup: SimpleNamespace) -> None:
    scope = run_cells("06_quafu_hardware.ipynb", enabled=True)
    record = json.loads(scope["guard_path"].read_text())
    record["circuit_hash"] = "different-circuit"
    scope["guard_path"].write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match="another experiment"):
        run_cells("06_quafu_hardware.ipynb", enabled=True)
    assert len(setup.calls) == 1


def test_hardware_default_and_explicit_target_override(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_cells("06_quafu_hardware.ipynb", enabled=True)
    assert setup.calls[0]["target"] == "quafu:Baihua"
    monkeypatch.setenv("QUAFU_TARGET", "quafu:Dongling")
    with pytest.raises(RuntimeError, match="another experiment"):
        run_cells("06_quafu_hardware.ipynb", enabled=True)
    assert len(setup.calls) == 1


def test_configured_hardware_target_is_used_for_a_fresh_experiment(
    setup: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUAFU_TARGET", "quafu:Dongling")
    run_cells("06_quafu_hardware.ipynb", enabled=True)
    assert setup.calls[0]["target"] == "quafu:Dongling"


def test_uncertain_attempt_cannot_be_retried(setup: SimpleNamespace) -> None:
    setup.outputs.mkdir()
    (setup.outputs / "quafu-bell.submission.json").write_text("{}")
    with pytest.raises(RuntimeError, match="do not resubmit"):
        run_cells("06_quafu_hardware.ipynb", enabled=True)
    assert setup.calls == []


def test_failed_job_is_not_hidden(setup: SimpleNamespace) -> None:
    setup.job.outcome = "failed"
    with pytest.raises(RuntimeError, match="Backend failed"):
        run_cells("06_quafu_hardware.ipynb", enabled=True)
    assert len(setup.calls) == 1


def test_save_failure_keeps_guard_and_prints_id(
    setup: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def reject(path: Path) -> None:
        raise OSError("Storage unavailable")

    monkeypatch.setattr(setup.job, "save", reject)
    with pytest.raises(OSError):
        run_cells("06_quafu_hardware.ipynb", enabled=True)
    assert "saved-task" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="do not resubmit"):
        run_cells("06_quafu_hardware.ipynb", enabled=True)
    assert len(setup.calls) == 1
