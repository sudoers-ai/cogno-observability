"""The ``py.typed`` marker reaches the BUILT WHEEL — the only place it does any work.

A marker that exists in the checkout and not in the wheel is worse than no marker: every reader of
the repo sees a typed package, and every INSTALL of it is untyped. PEP 561 is read from
site-packages, so the checkout's copy is never consulted by the consumer.

Measured on cogno-host 2026-09-18, which is why this file exists. That repo sets
``ignore_missing_imports = true`` (as every repo in this stack does, to keep third-party noise out
of its own errors), so an untyped dependency does not warn — it silently becomes ``Any`` and every
disagreement across the seam stops being an error. Over a real wheel of this package built without
the marker, ``mypy cogno_host`` reported the ``PrometheusMetricsSink``/``MetricsSink`` mismatch
**0 times**; with the marker present in that same installed wheel, **1**; removed again, **0**.

The build is deliberately done here rather than asserted off ``pyproject.toml``: the declaration
and the artefact are two different claims, and only the second one is the one that ships.

**IT BUILDS A COPY, AND THAT IS THE TEST'S OWN SCAR.** The first version built the repo in place
and passed over BOTH mutations that had to kill it — the ``package-data`` declaration removed, and
the marker file itself deleted. Cause: ``setuptools`` stages into ``build/lib`` and never removes
what disappeared, so every run after the first was unzipping a wheel assembled from a leftover.
Copying the tracked sources into a scratch directory removes the state that made the instrument
lie — and stops the test from writing ``build/`` into the working tree while it is at it.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import zipfile

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent
_SKIP = {".git", "build", "dist", ".mypy_cache", ".pytest_cache", ".ruff_cache", "__pycache__"}


def _pristine_copy(dest: pathlib.Path) -> pathlib.Path:
    """The repo without any build state — so the wheel is assembled from THESE files, not from
    whatever a previous build happened to stage."""
    def ignore(_dir, names):
        return [n for n in names if n in _SKIP or n.endswith(".egg-info")]
    shutil.copytree(_ROOT, dest, ignore=ignore)
    assert not (dest / "build").exists(), "the copy carried build state — the guard above failed"
    return dest


def test_py_typed_is_inside_the_built_wheel(tmp_path):
    src = _pristine_copy(tmp_path / "src")
    out = subprocess.run(
        # Build ISOLATION is left ON (pip's default): with `--no-build-isolation` the runner has to
        # already have the declared backend importable, and a CI virtualenv for 3.12 does not ship
        # `setuptools` — measured, `BackendUnavailable`, red on 3.12 and green on 3.11 in the same
        # matrix. An isolated build is also the faithful reproduction of how the wheel is really
        # made. `--no-cache-dir` stays: pip caches wheels built from a local directory, and with
        # the cache on, this test passed over a mutation that deleted the marker outright.
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "--no-cache-dir", "-w", str(tmp_path / "wheel"), str(src)],
        capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        pytest.fail("could not build a wheel, so this test measured nothing:\n"
                    + out.stdout[-3000:] + out.stderr[-3000:])

    wheels = sorted((tmp_path / "wheel").glob("cogno_observability-*.whl"))
    assert len(wheels) == 1, f"expected exactly one wheel, got {[w.name for w in wheels]}"
    names = zipfile.ZipFile(wheels[0]).namelist()
    assert "cogno_observability/py.typed" in names, (
        "the wheel carries no PEP 561 marker — a consumer that installs this package type-checks "
        f"NOTHING of it. Wheel contents: {sorted(names)}")
