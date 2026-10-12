"""Workshop job staging must not confuse image-local files with shared storage."""

import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
WORKSHOP = Path(__file__).resolve().parents[2] / "workshops" / "flagos2026"
SPEC = importlib.util.spec_from_file_location(
    "workshop_jiuding_paths", WORKSHOP / "scripts" / "jiuding_paths.py"
)
assert SPEC is not None and SPEC.loader is not None
HELPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HELPER)


def mounts(tmp_path: Path, *paths: Path) -> Path:
    table = tmp_path / "mountinfo"
    encoded = [str(path).replace(" ", r"\040") for path in paths]
    table.write_text(
        "".join(f"1 0 0:1 / {path} rw - nfs server:/data rw\n" for path in encoded),
        encoding="utf-8",
    )
    return table


@pytest.mark.parametrize("field", ["mountPath", "mountPoint", "path"])
def test_image_local_outputs_are_staged_on_configured_mount(
    tmp_path: Path, field: str
) -> None:
    shared = tmp_path / "shared folder"
    shared.mkdir()
    local = tmp_path / "image" / "outputs"
    local.mkdir(parents=True)
    program = "def main():\n    return {'ok': True}\n"
    script, receipt = HELPER.stage_job(
        program,
        [{field: str(shared)}],
        preferred=local,
        mountinfo=mounts(tmp_path, shared),
    )
    assert script.is_relative_to(shared)
    assert script.read_text() == program
    assert receipt.parent == script.parent
    assert not receipt.exists()
    assert script.parent.stat().st_mode & 0o777 == 0o700
    assert list(local.iterdir()) == []


@pytest.mark.parametrize("configured,mounted", [(False, True), (True, False)])
def test_both_platform_configuration_and_kernel_mount_are_required(
    tmp_path: Path, configured: bool, mounted: bool
) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    with pytest.raises(RuntimeError, match="No writable shared"):
        HELPER.stage_job(
            "program",
            [{"mountPath": str(shared)}] if configured else [],
            preferred=shared,
            mountinfo=mounts(tmp_path, *([shared] if mounted else [])),
        )
    assert list(shared.iterdir()) == []


def test_symlink_into_image_layer_and_root_mount_are_rejected(tmp_path: Path) -> None:
    local = tmp_path / "image"
    local.mkdir()
    alias = tmp_path / "shared"
    alias.symlink_to(local, target_is_directory=True)
    with pytest.raises(RuntimeError, match="No writable shared"):
        HELPER.stage_job(
            "program",
            [{"mountPath": str(alias)}, {"mountPath": "/"}],
            preferred=local,
            mountinfo=mounts(tmp_path, alias, Path("/")),
        )


def test_repeated_staging_preserves_existing_program_and_receipt(
    tmp_path: Path,
) -> None:
    table = mounts(tmp_path, tmp_path)
    storage = [{"mountPath": str(tmp_path)}]
    first, receipt = HELPER.stage_job(
        "first", storage, preferred=tmp_path, mountinfo=table
    )
    receipt.write_text("existing submission")
    second, other = HELPER.stage_job(
        "second", storage, preferred=tmp_path, mountinfo=table
    )
    assert first.parent != second.parent
    assert first.read_text() == "first"
    assert receipt.read_text() == "existing submission"
    assert not other.exists()


def test_preferred_project_is_created_only_under_verified_mount(tmp_path: Path) -> None:
    shared = tmp_path / "share" / "project"
    shared.mkdir(parents=True)
    project = shared / "flagquantum"
    script, receipt = HELPER.stage_job(
        "program",
        [{"mountPath": str(shared)}],
        preferred=project,
        mountinfo=mounts(tmp_path, shared),
    )
    assert script.is_relative_to(project)
    assert receipt.parent == script.parent


def test_notebook_submits_staged_paths_without_source_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    import types

    shared = tmp_path / "shared"
    shared.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    table = mounts(tmp_path, shared)
    helper = types.ModuleType("jiuding_paths")
    helper.stage_job = lambda program, storage_info, preferred: HELPER.stage_job(
        program, storage_info, preferred=preferred, mountinfo=table
    )
    monkeypatch.setitem(sys.modules, "jiuding_paths", helper)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    submitted = []

    class Client:
        def workspace(self) -> dict[str, object]:
            return {"storageInfo": [{"mountPath": str(shared)}]}

        def submit(self, script: Path, **kwargs: object) -> dict[str, str]:
            assert script.is_relative_to(shared)
            assert script.read_text() == "def main(): return 1"
            assert kwargs["image_region"] == "PRIVATE"
            assert "pythonpath" not in kwargs
            receipt = kwargs["receipt"]
            assert isinstance(receipt, Path) and receipt.parent == script.parent
            receipt.write_text('{"submission": "created"}')
            submitted.append(script)
            return {"submission": "created"}

    namespace = {
        "submit_job": True,
        "image": "example:v1",
        "image_region": "PRIVATE",
        "target": "jiuding:cpu",
        "job_python": "python3",
        "WORKSHOP": WORKSHOP,
        "Path": Path,
        "OUTPUTS": local,
        "program_text": "def main(): return 1",
        "client": Client(),
        "receipt_pointer": local / "latest.txt",
    }
    notebook = json.loads(
        (WORKSHOP / "notebooks/cloud/04_jiuding_jobs.ipynb").read_text()
    )
    source = next(
        "".join(cell["source"])
        for cell in notebook["cells"]
        if cell["cell_type"] == "code" and "if submit_job:" in "".join(cell["source"])
    )
    exec(source, namespace)
    assert len(submitted) == 1

    # A locally existing directory must not get as far as a submit request.
    class UnmountedClient(Client):
        def workspace(self) -> dict[str, object]:
            return {"storageInfo": [{"mountPath": str(local)}]}

    namespace["submit_job"] = True
    namespace["client"] = UnmountedClient()
    with pytest.raises(RuntimeError, match="No writable shared"):
        exec(source, namespace)
    assert len(submitted) == 1
    saved = Path(namespace["receipt_pointer"].read_text())
    assert json.loads(saved.read_text())["submission"] == "created"
    namespace["submit_job"] = False
    exec(source, namespace)
    assert len(submitted) == 1
