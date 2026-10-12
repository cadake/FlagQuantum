"""Personal workshop storage for the experimental Jiuding client."""

import errno
import fcntl
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Protocol


class JiudingSession(Protocol):
    """Existing experimental transport; credentials stay inside the client."""

    def _auth(self) -> dict[str, str]: ...

    def _request(
        self,
        path: str,
        body: dict[str, Any] | None,
        headers: dict[str, str],
        *,
        method: str = "POST",
    ) -> dict[str, Any]: ...

    def workspace(self) -> dict[str, Any]: ...


def _mounts(storage_info: object, mountinfo: Path) -> set[Path]:
    if not isinstance(storage_info, list) or not mountinfo.is_file():
        raise RuntimeError("Jiuding personal storage requires configured Linux mounts.")
    mounted: set[Path] = set()
    for line in mountinfo.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 6:
            mounted.add(
                Path(
                    re.sub(
                        r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), fields[4]
                    )
                )
            )
    candidates: set[Path] = set()
    for storage in storage_info:
        if not isinstance(storage, dict):
            continue
        for key in ("mountPath", "mountPoint", "path"):
            value = storage.get(key)
            if not isinstance(value, str) or not value.startswith("/"):
                continue
            path = Path(value)
            if (
                path != Path("/")
                and path in mounted
                and path.is_dir()
                and path.resolve() == path
            ):
                candidates.add(path)
    return candidates


def personal_directory(
    client: JiudingSession,
    *,
    mountinfo: Path = Path("/proc/self/mountinfo"),
    home_root: Path = Path("/home"),
    project: Path = Path("/share/project/flagquantum"),
) -> Path:
    """Prefer a personal home mount; otherwise namespace project storage by API ID.

    Uses the authenticated token's userinfo, never Linux UID or workspace owner
    as a substitute for identity. Per-user platform credentials and storage
    ACLs/mount isolation are prerequisites for security isolation.
    """
    data = client._request(
        "/api/v1/users/token/userinfo", None, client._auth(), method="GET"
    ).get("data")
    value = data.get("id") if isinstance(data, dict) else None
    if type(value) not in (str, int) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", str(value)
    ):
        raise RuntimeError(
            "Jiuding authenticated user has no safe platform ID; no files written."
        )
    uid = str(value)
    workspace = client.workspace()
    mounts = _mounts(workspace.get("storageInfo"), mountinfo)
    homes = sorted(path for path in mounts if path.parent == home_root)
    # A differently owned development environment is not the caller's home.
    personal = homes if str(workspace.get("creatorId")) == uid else []
    if len(personal) > 1:
        raise RuntimeError(
            "Multiple personal home mounts; instructor must disambiguate platform storage."
        )
    choices = [personal[0] / "flagquantum-workspace" / uid] if personal else []
    if any(project.is_relative_to(path) for path in mounts):
        choices.append(project / uid)
    for directory in choices:
        if directory.resolve() != directory:
            raise RuntimeError("Personal workspace must not contain symlinks.")
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            with tempfile.TemporaryFile(dir=directory):
                pass
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
                continue
            raise
        return directory
    raise RuntimeError(
        "No writable personal home or configured shared project mount. "
        "Ask the instructor to attach storage inherited by jobs at the same path; nothing submitted."
    )


def prepare_workshop(template: Path, directory: Path) -> Path:
    """Copy the template once; never overwrite a user's notebooks or outputs."""
    destination = directory / "workshop" / "flagos2026"
    lock_path = directory / ".workshop.lock"
    with os.fdopen(
        os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), "a"
    ) as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if destination.resolve() != destination:
            raise RuntimeError("Personal workshop must not contain symlinks.")
        if destination.exists():
            if not (destination / "notebooks").is_dir():
                raise RuntimeError(
                    "Existing personal workshop is incomplete; left unchanged."
                )
            return destination
        if not template.is_dir() or any(
            path.is_symlink() for path in template.rglob("*")
        ):
            raise RuntimeError("Workshop template is missing or contains symlinks.")
        destination.parent.mkdir(exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=directory) as staging:
            staged = Path(staging) / "flagos2026"
            shutil.copytree(
                template,
                staged,
                ignore=shutil.ignore_patterns(
                    "outputs", ".env", "__pycache__", ".ipynb_checkpoints"
                ),
            )
            staged.rename(destination)
    return destination


def stage_job(program: str, directory: Path) -> tuple[Path, Path]:
    """Create a fresh task directory inside the resolved personal workspace."""
    jobs = directory / "jobs"
    if jobs.resolve() != jobs:
        raise RuntimeError("Personal jobs directory must not contain symlinks.")
    jobs.mkdir(exist_ok=True, mode=0o700)
    run = Path(tempfile.mkdtemp(prefix="bell-", dir=jobs))
    script = run / "my_bell_job.py"
    try:
        script.write_text(program, encoding="utf-8")
    except OSError:
        script.unlink(missing_ok=True)
        run.rmdir()
        raise
    return script, run / "receipt.json"
