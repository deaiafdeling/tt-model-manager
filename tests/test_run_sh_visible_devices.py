# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 Tenstorrent USA, Inc.

"""run.sh chip selection: honour the operator, then a TT_VISIBLE_DEVICES grant, then the author, then 0..N-1.

Runs the real rendered run.sh with a stand-in interpreter that reports the chip env the
server would have started with.
"""

import os
import subprocess
import sys

import pytest

from tt_kernel import packaging
from tt_kernel.manifest import Mesh, WeightsRef

GRANT = "0000:01:00.0,0000:02:00.0,0000:03:00.0,0000:04:00.0"


def _bundle(tmp_path, *, devices, env=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    model_py = tmp_path / "model.py"
    model_py.write_text("class C: pass\n")
    b = tmp_path / "b"
    packaging.stage_thin_package(
        b, name="m", arch="blackhole", model_py=model_py, tt_kernel_version="0",
        vllm_metadata={"arch": "A", "main_class": "model:C"}, weights=WeightsRef(repo="o/w"),
        device_count=devices, mesh=Mesh(devices=devices, topology="P150"), env=env or {},
    )
    # Stand-in venv: `python -c` is real (run.sh's find_spec probe), `python -m ...` reports.
    (b / "venv" / "bin").mkdir(parents=True)
    py = b / "venv" / "bin" / "python"
    py.write_text(f'#!/bin/bash\n[ "$1" = -c ] && exec {sys.executable} "$@"\n'
                  'echo "TVD=${TT_VISIBLE_DEVICES:-} TMVD=${TT_METAL_VISIBLE_DEVICES:-}"\n')
    py.chmod(0o700)  # owner-only: the test is the only thing that runs it
    ttnn = tmp_path / "site" / "ttnn"
    (ttnn / "build" / "lib").mkdir(parents=True)
    (ttnn / "__init__.py").write_text("")
    (ttnn / "build" / "lib" / "_ttnncpp.so").write_text("")
    return b


def _run(b, **env):
    base = {k: v for k, v in os.environ.items()
            if k not in ("TT_VISIBLE_DEVICES", "TT_METAL_VISIBLE_DEVICES")}
    base["PYTHONPATH"] = str(b.parent / "site")
    r = subprocess.run(["bash", str(b / "run.sh")], env={**base, **env},
                       capture_output=True, text=True)
    return r.returncode, r.stdout.strip(), r.stderr


def test_defaults_to_the_first_n_chips(tmp_path):
    assert _run(_bundle(tmp_path, devices=4))[1] == "TVD= TMVD=0,1,2,3"
    assert _run(_bundle(tmp_path / "one", devices=1))[1] == "TVD= TMVD=0"


def test_a_grant_is_narrowed_to_the_chips_the_model_needs(tmp_path):
    code, out, _ = _run(_bundle(tmp_path, devices=1), TT_VISIBLE_DEVICES=GRANT)
    assert (code, out) == (0, "TVD=0000:01:00.0 TMVD=0")
    code, out, _ = _run(_bundle(tmp_path / "two", devices=2), TT_VISIBLE_DEVICES=GRANT)
    assert (code, out) == (0, "TVD=0000:01:00.0,0000:02:00.0 TMVD=0,1")


def test_a_grant_too_small_fails_fast(tmp_path):
    code, out, err = _run(_bundle(tmp_path, devices=4), TT_VISIBLE_DEVICES="0000:01:00.0")
    assert code != 0 and "TMVD" not in out
    assert "4 chip" in err and "TT_VISIBLE_DEVICES" in err


def test_operator_choice_wins(tmp_path):
    b = _bundle(tmp_path, devices=2, env={"TT_METAL_VISIBLE_DEVICES": "0,1"})
    assert _run(b, TT_METAL_VISIBLE_DEVICES="2,3")[1] == "TVD= TMVD=2,3"


def test_author_env_is_a_default_not_an_override(tmp_path):
    b = _bundle(tmp_path, devices=2, env={"TT_METAL_VISIBLE_DEVICES": "2,3"})
    assert _run(b)[1] == "TVD= TMVD=2,3"                      # used when nothing else says
    assert _run(b, TT_VISIBLE_DEVICES=GRANT)[1] == "TVD=0000:01:00.0,0000:02:00.0 TMVD=0,1"
    assert 'export TT_METAL_VISIBLE_DEVICES="2,3"' not in (b / "run.sh").read_text()
