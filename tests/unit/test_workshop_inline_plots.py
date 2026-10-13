"""Check that showcase plotting emits an inline image, even with an Agg default."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
NOTEBOOKS = Path(__file__).resolve().parents[2] / "workshops/flagos2026/notebooks"


@pytest.mark.parametrize(
    "path", sorted(NOTEBOOKS.glob("0[1-4]_*.ipynb")), ids=lambda p: p.stem
)
def test_plot_initialization_emits_inline_png(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nbformat = pytest.importorskip("nbformat")
    nbclient = pytest.importorskip("nbclient")
    pytest.importorskip("matplotlib")
    pytest.importorskip("ipykernel")
    notebook = json.loads(path.read_text(encoding="utf-8"))
    cells = [
        "".join(cell["source"])
        for cell in notebook["cells"]
        if cell["cell_type"] == "code"
    ]
    plotting = [cell for cell in cells if "import matplotlib.pyplot as plt" in cell]
    assert len(plotting) == 1
    initialization = plotting[0]
    assert initialization.index("%matplotlib inline") < initialization.index(
        "import matplotlib.pyplot as plt"
    )
    assert all("plt.show()" in cell for cell in cells if "plt.subplots(" in cell)
    monkeypatch.setenv("MPLBACKEND", "Agg")
    plotting_setup = "\n".join(
        line
        for line in initialization.splitlines()
        if line in {"%matplotlib inline", "import matplotlib.pyplot as plt"}
    )
    probe = nbformat.v4.new_notebook(
        cells=[
            nbformat.v4.new_code_cell(plotting_setup),
            nbformat.v4.new_code_cell("plt.plot([0, 1], [0, 1])\nplt.show()"),
        ]
    )
    nbclient.NotebookClient(
        probe,
        kernel_name="python3",
        timeout=60,
        resources={"metadata": {"path": str(tmp_path)}},
    ).execute()
    assert any(
        "image/png" in output.get("data", {}) for output in probe.cells[-1].outputs
    )
