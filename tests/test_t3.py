"""The T3 entry compiles to the JSON of the spikes, apart from the intended changes.

`cases/t3/system.py` and `cases/t3/spike-system.json` are the ACC1 entry and its compiled JSON (spike SG35, drvPath
`p9rl5qxp29h4pk22jhm26vdgxz4rbjk8`). The intended changes since then:

1. `console=None` is `serial_console=None` (009-D42). The entry hash changes with the text.
2. `trusted_interfaces` no longer writes the fragment `vendomat.sharing` (`trustedInterfaces`). The same list, empty or
   not, goes to the fragment `networking.firewall` at the same place (L125).
3. The module of `RemoteLogin` is `capabilities/remote-login/module.nix`, not `basix_config/native/remote-login.nix`.
4. The JSON holds `reads` (009-D45).
5. The JSON holds `provenance`: the option paths of each field and capability (`basix explain`, 009-D14).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from conftest import ROOT, run_command, run_compile

CASES = ROOT / "tests" / "cases" / "t3"


def test_t3_compiles_to_the_spike_json_apart_from_the_intended_changes(tmp_path: Path) -> None:
    text = (CASES / "system.txt").read_text().replace('console=None,  # "ttyS0"', 'serial_console=None,  # "ttyS0"')
    assert text != (CASES / "system.txt").read_text()
    (tmp_path / "system.py").write_text(text)
    compiled = run_compile(tmp_path)
    assert compiled.code == 0, compiled.err
    new = compiled.data
    old = json.loads((CASES / "spike-system.json").read_text())

    # Change 4 and the entry hash (change 1).
    assert new.pop("reads") == {"files": {}, "absent": [], "dirs": {}, "ignore": ["__pycache__", "system.json"]}
    assert new.pop("source_sha256") == hashlib.sha256(text.encode()).hexdigest()
    assert old.pop("source_sha256") == hashlib.sha256((CASES / "system.txt").read_bytes()).hexdigest()
    # Change 5: the key `provenance` (`basix explain`, 009-D14). Its test is in `test_machine.py`.
    assert set(new.pop("provenance")) >= {"RemoteLogin", "trusted_interfaces"}
    # Change 2.
    sharing = [f for f in old["fragments"] if f["path"] == ["vendomat", "sharing"]]
    assert len(sharing) == 1 and sharing[0]["value"] == {"trustedInterfaces": []}
    sharing[0]["path"] = ["networking", "firewall"]
    # Change 3.
    assert old["modules"] == [{"file": "basix_config/native/remote-login.nix", "source": "system.py:8 (RemoteLogin)"}]
    old["modules"] = [{"file": "capabilities/remote-login/module.nix", "source": "system.py:8 (RemoteLogin)"}]

    assert new == old


def test_t3_command_gives_the_same_bytes_as_the_in_process_compile(tmp_path: Path) -> None:
    """The real process and the two runs give the same JSON bytes."""
    text = (CASES / "system.txt").read_text().replace("console=None", "serial_console=None")
    (tmp_path / "system.py").write_text(text)
    assert run_compile(tmp_path).code == 0
    in_process = (tmp_path / "system.json").read_bytes()
    assert run_command(tmp_path).code == 0
    assert (tmp_path / "system.json").read_bytes() == in_process
