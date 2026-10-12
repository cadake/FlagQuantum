"""Workshop-only storage and recovery around the public fq.submit API."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from flagquantum import Circuit
    from flagquantum.remote.jobs import RemoteJob
    from flagquantum.runtime.result import ExecutionResult


class SubmissionRecord(TypedDict, total=False):
    circuit_hash: str
    target: str
    shots: int | None
    image: str | None
    project: str | None
    queue: str | None
    source: str
    status: str
    started_utc: str
    task_id: str
    counts: dict[str, int]


@dataclass(frozen=True)
class CloudLesson:
    """One teaching experiment; no methods submit unless explicitly enabled."""

    workshop: Path
    name: str

    @property
    def outputs(self) -> Path:
        return self.workshop / "outputs"

    @property
    def image(self) -> str:
        return os.getenv("JIUDING_IMAGE", "")

    @property
    def receipt(self) -> Path:
        return self.outputs / f"{self.name}.receipt.json"

    @property
    def record(self) -> Path:
        return self.outputs / f"{self.name}.json"

    def submit_once(
        self,
        circuit: Circuit,
        *,
        target: str,
        enabled: bool,
        shots: int | None = None,
        image: str | None = None,
    ) -> RemoteJob | None:
        """Submit through fq.submit or restore the matching saved job."""
        import flagquantum as fq
        from flagquantum.core.ir import ensure_circuit_ir

        if type(enabled) is not bool:
            raise TypeError("The submission switch must be True or False.")
        if not enabled:
            print(
                "Local rehearsal only. Enable the submission switch to create a real task."
            )
            return None
        if target.startswith("jiuding:"):
            if not image or not os.getenv("JIUDING_PROJECT"):
                raise RuntimeError(
                    "Ask your instructor to configure the Jiuding image and project; nothing submitted."
                )
        elif target.startswith("quafu:"):
            if not os.getenv("QUAFU_API_KEY"):
                raise RuntimeError(
                    "Configure Quafu access outside this notebook; nothing submitted."
                )
        else:
            raise ValueError("This lesson supports Jiuding and Quafu targets.")
        circuit_hash = ensure_circuit_ir(circuit).content_hash
        project = (
            os.getenv("JIUDING_PROJECT") if target.startswith("jiuding:") else None
        )
        queue = os.getenv("JIUDING_QUEUE") if target.startswith("jiuding:") else None
        identity = {
            "circuit_hash": circuit_hash,
            "target": target,
            "shots": shots,
            "image": image,
            "project": project,
            "queue": queue,
        }
        if self.receipt.exists():
            saved_record = json.loads(self.record.read_text())
            if not isinstance(saved_record, dict) or any(
                saved_record.get(key) != value for key, value in identity.items()
            ):
                raise RuntimeError(
                    "Saved task belongs to another experiment. Keep its files and use a different lesson name."
                )
            job = fq.restore_job(self.receipt)
            if saved_record.get("task_id") != job.id:
                raise RuntimeError(
                    "Task record and receipt disagree; ask your instructor to reconcile them."
                )
            print("Restored existing task; no new submission.")
            return job
        record: SubmissionRecord = {
            "circuit_hash": circuit_hash,
            "target": target,
            "shots": shots,
            "image": image,
            "project": project,
            "queue": queue,
            "source": "live",
            "status": "submission_started",
            "started_utc": datetime.now(timezone.utc).isoformat(),
        }
        # Exclusive reservation protects even an interrupted or uncertain POST.
        try:
            with self.record.open("x") as stream:
                json.dump(record, stream, indent=2)
        except FileExistsError as exc:
            raise RuntimeError(
                "An earlier attempt has no recovery receipt. Reconcile it on the platform; do not resubmit."
            ) from exc
        job = fq.submit(
            circuit,
            target=target,
            image=image,
            project=project,
            queue=queue,
            shots=shots,
            outputs=fq.counts() if target.startswith("quafu:") else None,
            compiler=None,
        )
        print("Submitted task:", job.id)
        # Save the ID before saving the full receipt, so a save failure is recoverable.
        record.update(task_id=job.id, status="submitted")
        self.record.write_text(json.dumps(record, indent=2))
        job.save(self.receipt)
        return job

    def collect(self, job: RemoteJob | None) -> ExecutionResult | None:
        """Wait briefly, preserving pending jobs and writing completed QPU counts."""
        if job is None:
            return None
        try:
            result = job.wait(timeout=30, poll_interval=3)
        except TimeoutError:
            print(
                "Task",
                job.id,
                "is still",
                job.raw_status,
                "— rerun this result cell later, not the submission.",
            )
            return None
        if job.target.startswith("quafu:"):
            record = json.loads(self.record.read_text())
            record.update(status="completed", counts=result.counts[0])
            self.record.write_text(json.dumps(record, indent=2))
        return result


def prepare_lesson(start: Path, name: str) -> CloudLesson:
    """Locate the workshop and protect platform users from editing its template."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
        raise ValueError("Lesson name must be a safe filename.")
    start = start.resolve()
    workshop = next(
        (
            root / "flagos2026"
            for root in (start, *start.parents)
            if (root / "flagos2026/notebooks").is_dir()
        ),
        None,
    )
    if workshop is None:
        raise RuntimeError(
            "Cannot locate flagos2026/notebooks. Open the deployed workshop first."
        )
    if Path("/etc/accesskey").exists() or os.getenv("JIUDING_AK"):
        from jiuding_paths import personal_directory, prepare_workshop

        from flagquantum.remote.compute.jiuding import JiudingClient

        personal = prepare_workshop(workshop, personal_directory(JiudingClient()))
        if not start.is_relative_to(personal):
            raise RuntimeError(
                f"Personal copy is ready. Open {personal / 'notebooks/cloud'}; the common template is unchanged."
            )
        workshop = personal
    lesson = CloudLesson(workshop, name)
    lesson.outputs.mkdir(parents=True, exist_ok=True)
    return lesson
