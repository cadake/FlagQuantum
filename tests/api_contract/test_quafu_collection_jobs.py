"""One POST, ordered asymmetric results, and restoration without resubmission."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest

import flagquantum as fq
from flagquantum.errors import CapabilityError, ExecutionError, ValidationError
from flagquantum.remote import _quafu_collection, jobs
from flagquantum.remote.qpu.quafu import QuafuProvider

pytestmark = pytest.mark.unit


class Transport:
    def __init__(self) -> None:
        self.posts: list[dict] = []
        self.gets: list[str] = []
        self.state = "Finished"
        self.maximum = 500
        self.error: Exception | None = None
        self.response = {"job_id": "collection-123"}
        self.pages = [self.page([2, 0]), self.page([1])]

    @staticmethod
    def page(indices: list[int]) -> dict:
        raw = [{"001": 1024}, {"010": 1024}, {"100": 1024}]
        return {
            "job_id": "collection-123",
            "seq": indices,
            "counts": [raw[index] for index in indices],
            "status": ["Finished"] * len(indices),
            "shots_returned": [1024] * len(indices),
            "n_clbits": [3] * len(indices),
            "bit_order": "c0_rightmost",
        }

    def post_json(self, url: str, payload: dict, headers: dict, timeout: float) -> dict:
        self.posts.append(dict(payload))
        if self.error is not None:
            raise self.error
        if url.endswith("/cancel"):
            self.state = "Cancelled"
            return {}
        assert url.endswith("/jobs")
        return dict(self.response)

    def get_json(self, url: str, headers: dict, timeout: float) -> dict:
        self.gets.append(url)
        if url.endswith("/metadata"):
            return {"limits": {"max_tasks_per_job": self.maximum}}
        if url.endswith("/status"):
            return {"status": self.state}
        if "/results?" in url:
            query = parse_qs(urlparse(url).query)
            assert query["fields"] == ["counts"]
            return copy.deepcopy(self.pages[0 if query["offset"] == ["0"] else 1])
        raise AssertionError(url)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    transport = Transport()

    class Client(QuafuProvider):
        def __init__(self, **kwargs: object) -> None:
            super().__init__(
                api_key="test-secret-never-in-receipt",
                task_server_url="https://quafu.test/api/v1",
                transport=transport,
                **kwargs,
            )

    monkeypatch.setattr(jobs, "QuafuProvider", Client)
    monkeypatch.setattr(_quafu_collection, "_PAGE_SIZE", 2)
    return SimpleNamespace(
        transport=transport,
        circuits=[fq.Circuit(3).x(wire) for wire in range(3)],
    )


def test_one_job_orders_paged_results_and_restores(
    api: SimpleNamespace,
    tmp_path: Path,
) -> None:
    job = fq.submit(
        api.circuits,
        target="quafu:Baihua",
        shots=1024,
        outputs=fq.counts(name="readout"),
        name="asymmetric collection",
    )
    payload = api.transport.posts[0]
    assert len(api.transport.posts) == 1
    assert payload["mode"] == "collection" and payload["circuit_level"] == "logical"
    assert payload["target"] == "Baihua" and payload["shots"] == 1024
    assert payload["name"] == "asymmetric collection"
    assert len(payload["circuits"]) == 3 and payload["client_ref"]
    for wire, item in enumerate(payload["circuits"]):
        assert f"x q[{wire}]" in item["qasm"]
    path = tmp_path / "collection.receipt.json"
    job.save(path)
    assert path.stat().st_mode & 0o777 == 0o600
    text = path.read_text()
    assert "test-secret" not in text and "OPENQASM" not in text
    with pytest.raises(FileExistsError):
        job.save(path)
    restored = fq.restore_job(path)
    result = restored.wait(timeout=0)
    expected = [{"100": 1024}, {"010": 1024}, {"001": 1024}]
    assert result.counts == expected
    for circuit, counts in zip(api.circuits, result.counts, strict=True):
        assert fq.run(circuit, shots=1024, outputs=fq.counts()).counts == [counts]
    assert restored.result().counts == expected
    assert result.measurements[0].metadata["name"] == "readout"
    assert result.runtime["shots_returned"] == [1024, 1024, 1024]
    assert "service_compiler" not in result.provenance
    assert [row["task_index"] for row in result.provenance["deployment"]] == [0, 1, 2]
    assert len(api.transport.posts) == 1


@pytest.mark.parametrize("state", ["PartiallyFinished", "Failed", "Cancelled"])
def test_failed_collection_is_not_partial_success(
    api: SimpleNamespace, state: str
) -> None:
    job = fq.submit(api.circuits, target="quafu:Baihua", shots=1024)
    api.transport.state = state
    with pytest.raises(ExecutionError, match=state):
        job.wait(timeout=0)
    assert not any("/results?" in url for url in api.transport.gets)
    assert len(api.transport.posts) == 1


def test_waiting_and_cancellation_do_not_submit_again(api: SimpleNamespace) -> None:
    job = fq.submit(api.circuits, target="quafu:Baihua", shots=1024)
    api.transport.state = "Queued"
    with pytest.raises(TimeoutError):
        job.wait(timeout=0)
    job.cancel()
    assert job.status() == "cancelled"
    assert len([p for p in api.transport.posts if p.get("mode") == "collection"]) == 1


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("response lost"),
        HTTPError(
            "https://quafu.test/api/v1/jobs",
            400,
            "rejected",
            {},
            None,
        ),
    ],
)
def test_uncertain_or_rejected_post_is_not_retried(
    api: SimpleNamespace,
    error: Exception,
) -> None:
    api.transport.error = error
    with pytest.raises(type(error)):
        fq.submit(api.circuits, target="quafu:Baihua", shots=1024)
    assert len(api.transport.posts) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"compiler": "qsteed"},
        {"target_qubits": [0, 1, 2]},
        {"devices": 2},
        {"image": "image"},
        {"shots": 1},
        {"outputs": fq.expectation(fq.Z(0))},
    ],
)
def test_unsupported_options_fail_before_post(
    api: SimpleNamespace, kwargs: dict
) -> None:
    options = {"target": "quafu:Baihua", "shots": 1024} | kwargs
    with pytest.raises((CapabilityError, TypeError, ValueError)):
        fq.submit(api.circuits, **options)
    assert api.transport.posts == [] and api.transport.gets == []


@pytest.mark.parametrize(
    "circuits,target",
    [
        ([], "quafu:Baihua"),
        ([fq.Circuit(2), fq.Circuit(3)], "quafu:Baihua"),
        ([object()], "quafu:Baihua"),
        ([fq.Circuit(3)], "jiuding:cpu"),
    ],
)
def test_invalid_collection_fails_before_network(
    api: SimpleNamespace,
    circuits: list,
    target: str,
) -> None:
    with pytest.raises((CapabilityError, ValueError, TypeError)):
        fq.submit(circuits, target=target, shots=1024)
    assert api.transport.posts == [] and api.transport.gets == []


@pytest.mark.parametrize("maximum", [2, None, True, 0])
def test_published_limit_is_required_before_post(
    api: SimpleNamespace, maximum: object
) -> None:
    api.transport.maximum = maximum
    with pytest.raises(CapabilityError, match="max_tasks_per_job"):
        fq.submit(api.circuits, target="quafu:Baihua", shots=1024)
    assert api.transport.posts == []


def test_missing_task_api_key_never_uses_legacy(
    api: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = jobs.QuafuProvider

    def without_key(**kwargs: object) -> QuafuProvider:
        client = factory(**kwargs)
        client.api_key = None
        return client

    monkeypatch.setattr(jobs, "QuafuProvider", without_key)
    with pytest.raises(ValidationError, match="QUAFU_API_KEY"):
        fq.submit(api.circuits, target="quafu:Baihua", shots=1024)
    assert api.transport.posts == [] and api.transport.gets == []


@pytest.mark.parametrize(
    "change",
    [
        "missing_context",
        "schema",
        "width",
        "digest",
        "reorder",
        "shots",
        "identity",
    ],
)
def test_corrupt_receipt_is_rejected_without_network(
    api: SimpleNamespace,
    tmp_path: Path,
    change: str,
) -> None:
    job = fq.submit(api.circuits, target="quafu:Baihua", shots=1024)
    path = tmp_path / "receipt.json"
    job.save(path)
    raw = json.loads(path.read_text())
    if change == "missing_context":
        raw["native_receipt"] = None
    elif change == "schema":
        raw["native_receipt"]["schema"] = "future"
    elif change == "width":
        raw["n_wires"] = 2
    elif change == "digest":
        raw["native_receipt"]["sha256"] = "forged"
    elif change == "reorder":
        raw["native_receipt"]["items"].reverse()
    elif change == "shots":
        raw["native_receipt"]["shots"] = True
    else:
        raw["native_receipt"]["items"][0] = {"unexpected": 1}
    path.write_text(json.dumps(raw))
    before = list(api.transport.gets)
    with pytest.raises(ValueError):
        fq.restore_job(path)
    assert api.transport.gets == before and len(api.transport.posts) == 1


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "out_of_range",
        "missing",
        "alignment",
        "bit_order",
        "negative",
        "fractional",
        "boolean",
        "width",
        "shots",
        "failed",
        "job_id",
        "unavailable",
    ],
)
def test_malformed_or_unavailable_rows_never_return_success(
    api: SimpleNamespace,
    change: str,
) -> None:
    job = fq.submit(api.circuits, target="quafu:Baihua", shots=1024)
    page = api.transport.pages[0]
    if change == "duplicate":
        page["seq"] = [0, 0]
    elif change == "out_of_range":
        page["seq"] = [3, 0]
    elif change == "missing":
        api.transport.pages[1]["seq"] = []
    elif change == "alignment":
        page["counts"] = []
    elif change == "bit_order":
        page["bit_order"] = "unknown"
    elif change in {"negative", "fractional", "boolean"}:
        page["counts"][0] = {
            "100": {"negative": -1, "fractional": 0.5, "boolean": True}[change]
        }
    elif change == "width":
        page["n_clbits"][0] = 2
    elif change == "shots":
        page["shots_returned"][0] = 1023
    elif change == "failed":
        page["status"][0] = "Failed"
    elif change == "job_id":
        page["job_id"] = "other-job"
    else:
        page["counts"][0] = None
    with pytest.raises(RuntimeError):
        job.result()
    assert len(api.transport.posts) == 1


def test_one_element_collection_and_leftmost_counts(api: SimpleNamespace) -> None:
    api.transport.pages = [Transport.page([0])]
    api.transport.pages[0]["bit_order"] = "c0_leftmost"
    api.transport.pages[0]["counts"] = [{"100": 1024}]
    job = fq.submit(api.circuits[:1], target="quafu:Baihua", shots=1024)
    assert api.transport.posts[0]["mode"] == "collection"
    assert job.result().counts == [{"100": 1024}]


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"job_id": ""},
        {"job_id": "accepted", "n_tasks": True},
        {"job_id": "accepted", "n_tasks": 2},
        {"job_id": "accepted", "task_ids": ["only-one"]},
    ],
)
def test_invalid_submission_response_never_retries(
    api: SimpleNamespace, response: dict
) -> None:
    api.transport.response = response
    with pytest.raises(ExecutionError, match="do not resubmit"):
        fq.submit(api.circuits, target="quafu:Baihua", shots=1024)
    assert len(api.transport.posts) == 1


def test_returned_shots_remain_per_circuit(api: SimpleNamespace) -> None:
    api.transport.pages[0]["counts"][0] = {"100": 1000}
    api.transport.pages[0]["shots_returned"][0] = 1000
    job = fq.submit(api.circuits, target="quafu:Baihua", shots=1024)
    result = job.result()
    assert result.runtime["shots_returned"] == [1024, 1024, 1000]
    assert result.measurements[0].shots is None
    assert sum(result.counts[2].values()) == 1000
