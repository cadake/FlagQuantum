from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
WORKSHOP = Path(__file__).resolve().parents[2] / "workshops/flagos2026"
MAIN = (
    "basics/01_first_circuit.ipynb",
    "training/03_quantum_training.ipynb",
    "training/07_hybrid_model.ipynb",
    "simulation/18_one_problem_three_simulators.ipynb",
    "cloud/04_jiuding_jobs.ipynb",
    "cloud/05_quafu_hardware.ipynb",
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
        for node in ast.walk(ast.parse(code))
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
    if relative.startswith("cloud/"):
        assert "fq.submit(" in code and "fq.restore_job(" in code
        assert "job.save(" in code and "job.status()" in code and "job.wait(" in code
    assert f"notebooks/{relative}" in (WORKSHOP / "README.md").read_text()
