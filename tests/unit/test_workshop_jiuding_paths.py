"""Authenticated identities, not container UIDs, partition workshop storage."""

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit
WORKSHOP = Path(__file__).resolve().parents[2] / "workshops" / "flagos2026"
SPEC = importlib.util.spec_from_file_location(
    "paths", WORKSHOP / "scripts/jiuding_paths.py"
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


class Session:
    def __init__(
        self, uid: object, storage: list[dict[str, str]], owner: str = "owner"
    ) -> None:
        self.uid, self.storage, self.owner = uid, storage, owner

    def _auth(self) -> dict[str, str]:
        return {"AIRS-Token": "test-token"}

    def _request(
        self, path: str, body: object, headers: object, *, method: str = "POST"
    ) -> dict[str, object]:
        assert path == "/api/v1/users/token/userinfo"
        assert method == "GET" and body is None and headers == self._auth()
        return {"data": {"id": self.uid}}

    def workspace(self) -> dict[str, object]:
        return {"creatorId": self.owner, "storageInfo": self.storage}


def mounts(tmp_path: Path, *paths: Path) -> Path:
    table = tmp_path / "mountinfo"
    encoded = [str(path).replace(" ", r"\040") for path in paths]
    table.write_text(
        "".join(f"1 0 0:1 / {p} rw - nfs server:/data rw\n" for p in encoded)
    )
    return table


@pytest.mark.parametrize("field", ["mountPath", "mountPoint", "path"])
def test_shared_project_partitions_two_platform_users(
    tmp_path: Path, field: str
) -> None:
    shared = tmp_path / "shared folder"
    shared.mkdir()
    table = mounts(tmp_path, shared)
    project = shared / "flagquantum"
    directories = [
        HELPER.personal_directory(
            Session(uid, [{field: str(shared)}]),
            mountinfo=table,
            home_root=tmp_path / "home",
            project=project,
        )
        for uid in ("alice-id", "bob-id")
    ]
    assert directories == [project / "alice-id", project / "bob-id"]
    receipts = []
    for index, directory in enumerate(directories):
        script, receipt = HELPER.stage_job(f"program-{index}", directory)
        receipt.write_text(f"receipt-{index}")
        receipts.append(receipt)
        assert script.read_text() == f"program-{index}"
        assert script.parent.stat().st_mode & 0o777 == 0o700
    assert receipts[0].read_text() == "receipt-0"
    assert receipts[1].read_text() == "receipt-1"


def test_personal_home_is_preferred_and_wrong_workspace_owner_is_not_identity(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home" / "personal"
    project = tmp_path / "share" / "flagquantum"
    home.mkdir(parents=True)
    project.mkdir(parents=True)
    storage = [{"mountPath": str(p)} for p in (home, project)]
    table = mounts(tmp_path, home, project)
    own = HELPER.personal_directory(
        Session("7", storage, owner="7"),
        mountinfo=table,
        home_root=home.parent,
        project=project,
    )
    assert own == home / "flagquantum-workspace" / "7"
    other = HELPER.personal_directory(
        Session("8", storage, owner="7"),
        mountinfo=table,
        home_root=home.parent,
        project=project,
    )
    assert other == project / "8"
    assert not (project / "7").exists()


@pytest.mark.parametrize(
    "uid", [None, True, "", "../other", "/root", "a/b", ".", "..", "a" * 129]
)
def test_invalid_identity_fails_before_writing(tmp_path: Path, uid: object) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(RuntimeError, match="safe platform ID"):
        HELPER.personal_directory(
            Session(uid, [{"path": str(project)}]),
            mountinfo=mounts(tmp_path, project),
            project=project,
        )
    assert list(project.iterdir()) == []


@pytest.mark.parametrize("configured,mounted", [(False, True), (True, False)])
def test_requires_configuration_and_real_mount(
    tmp_path: Path, configured: bool, mounted: bool
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(RuntimeError, match="No writable personal"):
        HELPER.personal_directory(
            Session("7", [{"path": str(project)}] if configured else []),
            mountinfo=mounts(tmp_path, *([project] if mounted else [])),
            project=project,
        )
    assert list(project.iterdir()) == []


def test_root_mount_and_symlink_namespace_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="No writable personal"):
        HELPER.personal_directory(
            Session("7", [{"path": "/"}]),
            mountinfo=mounts(tmp_path, Path("/")),
            project=tmp_path / "project",
        )
    project = tmp_path / "project"
    project.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    (project / "7").symlink_to(other, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symlinks"):
        HELPER.personal_directory(
            Session("7", [{"path": str(project)}]),
            mountinfo=mounts(tmp_path, project),
            project=project,
        )
    assert list(other.iterdir()) == []


def test_personal_copies_and_receipt_pointers_do_not_overlap(tmp_path: Path) -> None:
    template = tmp_path / "template"
    (template / "notebooks").mkdir(parents=True)
    (template / "notebooks" / "lab.ipynb").write_text("template")
    (template / "outputs").mkdir()
    (template / "outputs" / "someone-else.txt").write_text("private")
    (template / ".env").write_text("do not copy")
    copies = []
    for uid in ("alice", "bob"):
        directory = tmp_path / uid
        directory.mkdir()
        copy = HELPER.prepare_workshop(template, directory)
        copies.append(copy)
        (copy / "outputs").mkdir()
        (copy / "outputs" / "latest.txt").write_text(uid)
        assert not (copy / ".env").exists()
        assert not (copy / "outputs/someone-else.txt").exists()
    (copies[0] / "notebooks/lab.ipynb").write_text("alice edited")
    HELPER.prepare_workshop(template, tmp_path / "alice")
    assert (copies[0] / "notebooks/lab.ipynb").read_text() == "alice edited"
    assert (copies[1] / "notebooks/lab.ipynb").read_text() == "template"
    assert (copies[0] / "outputs/latest.txt").read_text() == "alice"
    assert (copies[1] / "outputs/latest.txt").read_text() == "bob"


def test_jobs_are_fresh_and_preserve_existing_receipts(tmp_path: Path) -> None:
    script, receipt = HELPER.stage_job("first", tmp_path)
    receipt.write_text("keep")
    next_script, next_receipt = HELPER.stage_job("second", tmp_path)
    assert script.parent != next_script.parent
    assert script.read_text() == "first" and receipt.read_text() == "keep"
    assert not next_receipt.exists()


def test_notebook_refuses_common_template_and_uses_personal_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "flagos2026"
    notebook_dir = template / "notebooks/cloud"
    notebook_dir.mkdir(parents=True)
    (notebook_dir / "04_jiuding_jobs.ipynb").write_text("template")
    project = tmp_path / "project"
    project.mkdir()
    session = Session("alice", [{"path": str(project)}])
    table = mounts(tmp_path, project)
    helper = types.ModuleType("jiuding_paths")
    helper.personal_directory = lambda client: HELPER.personal_directory(
        client, mountinfo=table, project=project
    )
    helper.prepare_workshop = HELPER.prepare_workshop
    helper.stage_job = HELPER.stage_job
    monkeypatch.setitem(sys.modules, "jiuding_paths", helper)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("JIUDING_AK", "test-presence-only")
    exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda path: path == Path("/etc/accesskey") or exists(path)
    )
    import flagquantum.remote.compute.jiuding as jiuding

    monkeypatch.setattr(jiuding, "JiudingClient", lambda: session)
    notebook = json.loads(
        (WORKSHOP / "notebooks/cloud/04_jiuding_jobs.ipynb").read_text()
    )
    locator = next(
        "".join(c["source"])
        for c in notebook["cells"]
        if "search_roots" in "".join(c["source"])
    )
    monkeypatch.chdir(notebook_dir)
    with pytest.raises(RuntimeError, match="Personal copy is ready"):
        exec(locator, {})
    assert not (template / "outputs").exists()
    personal = project / "alice" / "workshop/flagos2026"
    monkeypatch.chdir(personal / "notebooks/cloud")
    namespace: dict[str, Any] = {}
    exec(locator, namespace)
    assert namespace["OUTPUTS"] == personal / "outputs"
    assert namespace["personal_workspace"] == project / "alice"

    def submit(script: Path, **kwargs: object) -> dict[str, str]:
        assert script.is_relative_to(project / "alice" / "jobs")
        assert script.read_text() == "def main(): return 1"
        assert kwargs["image_region"] == "PRIVATE" and "pythonpath" not in kwargs
        receipt = kwargs["receipt"]
        assert isinstance(receipt, Path)
        receipt.write_text('{"submission": "created"}')
        return {"submission": "created"}

    client = types.SimpleNamespace(submit=submit)
    pointer = namespace["OUTPUTS"] / "my_bell_job.latest_receipt_path.txt"
    namespace.update(
        submit_job=True,
        image="image:v1",
        image_region="PRIVATE",
        target="jiuding:cpu",
        job_python="python3",
        client=client,
        program_text="def main(): return 1",
        receipt_pointer=pointer,
    )
    submission = next(
        "".join(c["source"])
        for c in notebook["cells"]
        if c["cell_type"] == "code" and "if submit_job:" in "".join(c["source"])
    )
    exec(submission, namespace)
    receipt = Path(pointer.read_text())
    assert receipt.is_relative_to(project / "alice")
    assert json.loads(receipt.read_text())["submission"] == "created"
    assert not (template / "outputs").exists()
