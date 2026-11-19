"""Real-body controls for the QA gate: bodies the Seeno lead PASSed or FAILed
by hand on 11-18 must come out of the gate the same way (ruling L57062:
look FAILs are warnings with a score under 100, not blocks).

Reads QA_GATE_REAL_BODIES_TSV (tab-separated; '#' comments):
  name  path  flags  expect_ok  must_warn  must_block
flags: comma list of still, fade, audio=allowed, fps=<n>, target=<seconds>,
       anysize (a provider's native frame), sideways (the 3-frame picture test),
       repair (copy to tmp and run the SAR repair); '-' for none.
must_warn / must_block: comma list of codes that must be present, or '-'.
Skipped entirely when the variable is unset or the file is missing; a row
whose body is not on this machine is skipped on its own.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from services import qa_gate


def _rows():
    raw = os.environ.get("QA_GATE_REAL_BODIES_TSV", "").strip()
    if not raw or not Path(raw).is_file():
        return []
    rows = []
    for line in Path(raw).read_text("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        name, path, flags, expect_ok, must_warn, must_block = line.rstrip("\n").split("\t")
        rows.append(pytest.param(path, flags, expect_ok == "1",
                                 [] if must_warn == "-" else must_warn.split(","),
                                 [] if must_block == "-" else must_block.split(","), id=name))
    return rows


def _policy(flags: str) -> tuple[qa_gate.QaPolicy, bool]:
    fields: dict = {}
    repair = False
    fps = 30.0
    target = None
    for flag in ([] if flags == "-" else flags.split(",")):
        key, _, value = flag.partition("=")
        if key == "still":
            fields["still"] = True
        elif key == "fade":
            fields["fade"] = True
        elif key == "audio":
            fields["audio"] = value
        elif key == "fps":
            fps = float(value)
        elif key == "target":
            target = float(value)
        elif key == "repair":
            repair = True
        elif key == "sound":
            fields["sound_id"] = value
        elif key == "anysize":
            fields["width"] = fields["height"] = None
        elif key == "sideways":
            fields["sideways_check"] = True
    fields["fps"] = fps
    if target is not None:
        fields["expected_frames"] = qa_gate.frames_for(target, fps)
    return qa_gate.QaPolicy(**fields), repair


@pytest.mark.parametrize("path,flags,expect_ok,must_warn,must_block", _rows())
def test_real_body_matches_the_leads_verdict(tmp_path, path, flags, expect_ok, must_warn, must_block):
    body = Path(path).expanduser()
    if not body.is_file():
        pytest.skip(f"body not on this machine: {body}")
    policy, repair = _policy(flags)
    if repair:
        # Never write to the archive: the repair runs on a private copy.
        copy = tmp_path / body.name
        shutil.copyfile(body, copy)
        verdict = qa_gate.validate_and_repair(copy, policy)
        assert verdict.repaired_sar
    else:
        verdict = qa_gate.validate_clip(body, policy)
    warnings = [item.code for item in verdict.warnings]
    blocks = [item.code for item in verdict.blocks]
    assert verdict.ok is expect_ok, (blocks, warnings, [item.detail for item in verdict.findings])
    for code in must_warn:
        assert code in warnings, (warnings, [item.detail for item in verdict.findings])
    for code in must_block:
        assert code in blocks, (blocks, [item.detail for item in verdict.findings])
    if expect_ok and not must_warn:
        assert verdict.score == 100, warnings
    if must_warn:
        assert verdict.score < 100
