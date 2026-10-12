"""Stage workshop jobs on storage inherited by Jiuding experiment containers."""

import re
import tempfile
from pathlib import Path


def stage_job(
    program: str,
    storage_info: object,
    *,
    preferred: Path,
    mountinfo: Path = Path("/proc/self/mountinfo"),
) -> tuple[Path, Path]:
    """Return a shared script and a fresh receipt path without submitting a job.

    Only configured container mount paths also present in the kernel mount
    table are eligible. Directory existence alone is not shared-storage proof.
    Jiuding must inherit these mounts at the same paths in the job container.
    """
    if not isinstance(storage_info, list) or not mountinfo.is_file():
        raise RuntimeError(
            "Jiuding job staging requires configured Linux shared mounts."
        )
    mounted = set()
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 6:
            decoded = re.sub(
                r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]
            )
            mounted.add(Path(decoded))
    candidates: set[Path] = set()
    for storage in storage_info:
        if not isinstance(storage, dict):
            continue
        for key in ("mountPath", "mountPoint", "path"):
            value = storage.get(key)
            if not isinstance(value, str) or not value.startswith("/"):
                continue
            path = Path(value)
            # Never accept the image root or a symlink into its private layer.
            if (
                path != Path("/")
                and path in mounted
                and path.is_dir()
                and path.resolve() == path
            ):
                candidates.add(path)
    preferred = preferred.resolve()
    ordered = sorted(
        candidates,
        key=lambda path: (
            not preferred.is_relative_to(path),
            not str(path).startswith("/home/"),
            -len(path.parts),
            str(path),
        ),
    )
    for root in ordered:
        destination = preferred if preferred.is_relative_to(root) else root
        try:
            destination.mkdir(parents=True, exist_ok=True)
            directory = Path(
                tempfile.mkdtemp(prefix="flagquantum-bell-", dir=destination)
            )
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            continue
        script = directory / "my_bell_job.py"
        try:
            script.write_text(program, encoding="utf-8")
        except OSError:
            script.unlink(missing_ok=True)
            directory.rmdir()
            raise
        return script, directory / "receipt.json"
    raise RuntimeError(
        "No writable shared container mount was verified in Jiuding storageInfo. "
        "Ask the instructor to attach storage to this development environment "
        "and its jobs at the same path; nothing was submitted."
    )
