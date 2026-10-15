# Remote

Adapt resources reached through an external task control plane: QPUs, GPU/HPC
services, and cloud platforms. The boundary is the submit/status/result
lifecycle; directly controlled devices belong in Compute.

Adapters own credentials, discovery, submission, polling, cancellation, and
result decoding. They translate external data into FlagQuantum-owned contracts.
Compilation, simulation, Runtime policy, and deployment-package semantics stay
with their owning domains.

## Choose an adapter

- [QPU adapters](qpu/): import providers from `flagquantum.remote.qpu` for
  quantum submissions, counts, and provider evidence.
- [Remote compute](compute/README.md): import `JiudingClient` from
  `flagquantum.remote.compute` for batch jobs and reusable Jiuding workspaces.
- [Quafu guide](../../docs/guides/QUAFU_BACKEND.md): compiler, token, and hardware setup.
- [Jiuding guide](../../docs/guides/JIUDING.md): jobs, workspace execution, and recovery.

Keep classical compute results distinct from the QPU shots/counts contract.
A provider response must match its submitted program, target, and task; an
ambiguous submission must not be silently retried.

`fq.submit([circuit0, circuit1], target="quafu:...", shots=1024)` creates one
Quafu collection, not separate parent jobs. The first slice requires same-width
logical circuits and full-register counts; `job.result().counts` follows input
order. See the [remote job guide](../../docs/guides/REMOTE_JOBS.md) for receipt,
failure and quota semantics. A mock transport test is not hardware acceptance.

Run offline adapter checks from the repository root:

```bash
python -m pytest tests/test_amazon_braket_provider.py tests/test_cloud_providers.py \
  tests/test_quafu_calibration.py tests/team/remote -q
```

Live acceptance is explicit and target-specific; test doubles do not certify
provider hardware.
