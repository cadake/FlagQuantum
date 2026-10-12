# Cloud job field notes

Notebooks 04 and 05 teach one circuit workflow each. Their workshop-only
`lesson.submit_once()` shortcut calls the public `fq.submit()` API and keeps
credential-free receipts automatically. It restores the same experiment on a
rerun, refuses changed circuit/configuration under the same name, and blocks an
uncertain submission rather than retrying a POST. `lesson.collect()` waits for
30 seconds of polling; pending work remains live. Network calls have their own
bounded timeouts. Errors are not replaced by local simulation results.

## The public API outside the workshop

The following examples create real tasks. Configure access outside Python source
and confirm the resource budget first. Never print API keys or AK/SK.

```python
import flagquantum as fq

circuit = fq.Circuit(2).h(0).cx(0, 1)
job = fq.submit(circuit, target="quafu:Dongling", shots=1024, outputs=fq.counts())
job.save("bell.receipt.json")
print(job.id, job.status())
```

Saving a receipt never overwrites an existing file. Later, including after a
kernel restart:

```python
job = fq.restore_job("bell.receipt.json")
print(job.status())
if job.status() == "succeeded":
    print(job.result().counts)
```

For Jiuding, supply a compatible image and project. A public image can be used
when Jiuding supports its registry/catalog entry; public availability alone does
not prove it contains FlagQuantum or can run on the selected queue. The workshop
uses the instructor's verified `JIUDING_IMAGE` instead of an unverified fallback.

```python
import os

job = fq.submit(
    circuit,
    target="jiuding:cpu",
    image=os.environ["JIUDING_IMAGE"],
    project=os.environ["JIUDING_PROJECT"],
    queue=os.environ["JIUDING_QUEUE"],
)
job.save("cpu-bell.receipt.json")
```

For an accelerator, select a supported `jiuding:gpu/<model>` target, matching
image and queue, and `devices=N`. CPU `devices=1` does not mean one accelerator
card. Allocation alone does not prove distributed speedup or capacity expansion.
Consult the Jiuding guide and current runtime limitations before changing this.

## Complete Python programs

The program defines `main()` and returns a JSON-serializable result. Its program and output paths must
be visible through the same shared mount inside both containers; image-local
paths such as `/opt/flagquantum` are not shared storage. The personal-directory
and staging helpers in `scripts/jiuding_paths.py` verify platform identity and
mounted storage and preserve separate job directories. For example, in a configured
Jiuding development environment:

```python
from jiuding_paths import personal_directory, stage_job
from flagquantum.remote.compute.jiuding import JiudingClient

client = JiudingClient()
directory = personal_directory(client)
program = """import flagquantum as fq

def main():
    state = fq.run(fq.Circuit(2).h(0).cx(0, 1)).state
    return {"probabilities": state.abs().square().tolist()}
"""
script, receipt_path = stage_job(program, directory)
receipt = client.submit(
    script, target="jiuding:cpu", image=os.environ["JIUDING_IMAGE"],
    python=os.getenv("JIUDING_JOB_PYTHON", "python3"),
    receipt=receipt_path, cpus=2, memory_gib=2,
)
```

Unlike the circuit lesson shortcut, each deliberate invocation of this script
submission creates a separate staged job. Reuse `receipt` with `client.status()`
and `client.result()` to inspect that job, not another submission invocation.

## Recovery

- Queued/running: rerun the status or result cell, not a new submission.
- Saved receipt: restore it; no method on the restored job resubmits work.
- Task ID printed but no receipt: inspect that ID on the platform and reconcile
  the interrupted save. Keep the reserved result file; do not delete it to retry.
- Failed/cancelled: inspect provider logs. There is no automatic retry.
- Different experiment: use a new lesson name and retain previous records.

Personal folder names prevent accidental collisions; security isolation still
requires platform ACLs and individually scoped credentials. A directory name
alone is not an access-control boundary.
