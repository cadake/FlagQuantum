from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
WORKSHOP = Path(__file__).resolve().parents[2] / "workshops/flagos2026"
MAIN = (
    "01_first_circuit.ipynb",
    "02_quantum_training.ipynb",
    "03_hybrid_model.ipynb",
    "04_one_problem_three_simulators.ipynb",
    "05_jiuding_jobs.ipynb",
    "06_quafu_hardware.ipynb",
)


def test_main_course_is_flat_and_sequential() -> None:
    root = WORKSHOP / "notebooks"
    assert sorted(p.name for p in root.iterdir()) == list(MAIN)
    assert len(list((WORKSHOP / "reference_notebooks").rglob("*.ipynb"))) == 12
    for index, filename in enumerate(MAIN, start=1):
        notebook = json.loads((root / filename).read_text())
        assert "".join(notebook["cells"][0]["source"]).startswith(f"# {index:02}:")


def test_workshop_relative_links_survive_moves() -> None:
    for path in WORKSHOP.rglob("*.ipynb"):
        if path.parent.name in {"outputs", "validation"}:
            continue
        notebook = json.loads(path.read_text())
        for cell in notebook["cells"]:
            if cell["cell_type"] != "markdown":
                continue
            for target in re.findall(r"\]\(([^)]+)\)", "".join(cell["source"])):
                if ":" not in target and not target.startswith("#"):
                    assert (path.parent / target.split("#")[0]).exists(), (
                        path,
                        target,
                    )


@pytest.mark.parametrize("relative", MAIN)
def test_showcase_uses_native_apis_and_copyable_python(relative: str) -> None:
    notebook = json.loads((WORKSHOP / "notebooks" / relative).read_text())
    code = "\n".join(
        "".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code"
    )
    assert "import flagquantum as fq" in code
    assert not any(
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "lesson"
        for node in ast.walk(
            ast.parse(
                code.replace(
                    "%matplotlib inline",
                    'get_ipython().run_line_magic("matplotlib", "inline")',
                )
            )
        )
    )
    assert "submit_once(" not in code
    ending = "".join(notebook["cells"][-1]["source"])
    assert ending.startswith("## Take it home")
    snippets = re.findall(r"```python\n(.*?)```", ending, re.DOTALL)
    assert snippets
    for snippet in snippets:
        ast.parse(snippet)
        assert "import flagquantum as fq" in snippet
        assert "cloud_lesson" not in snippet and "WORKSHOP" not in snippet
        assert "/home/" not in snippet and "/share/" not in snippet
    if relative in MAIN[-2:]:
        assert "fq.submit(" in code and "fq.restore_job(" in code
        assert "job.save(" in code and "job.status()" in code and "job.wait(" in code
    assert f"notebooks/{relative}" in (WORKSHOP / "README.md").read_text()
