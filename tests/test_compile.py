"""The compiler: error lines, the read audit, the double run, `discover`, and the pair check.

Each case runs `python -m basix_config.compile system.py system.json` in a scratch workspace, as the command does.
The error lines are the proven texts of the S10 cases and of the spikes SG31 to SG44 (CONCEPT 7).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from conftest import ROOT, Compiled, machine_text, run_command, run_compile

KEY = "example.cachix.org-1:" + "A" * 43 + "="


def one(compiled: Compiled) -> str:
    """The only line of an error."""
    assert compiled.code == 1, compiled.err
    assert len(compiled.lines) == 1, compiled.err
    return compiled.lines[0]


class TestErrorLines:
    """One line each, `FILE:LINE:COLUMN: ...`, with no traceback."""

    def test_user_must_be_an_account_name(self, workspace) -> None:
        text = machine_text().replace('user="learner"', "user=42")
        assert one(workspace(text)) == (
            "system.py:5:10: Machine.user must be an account name "
            "(a lower case letter, then up to 31 lower case letters, digits, or '-'), found 42."
        )

    def test_missing_user_names_the_help_of_the_field(self, workspace) -> None:
        text = machine_text().replace('    user="learner",\n', "")
        assert one(workspace(text)) == (
            "system.py:3:11: Machine.user is required. "
            "Your account name: lower-case letters, digits, and '-', at most 32 characters."
        )

    def test_unknown_setting(self, workspace) -> None:
        assert one(workspace(machine_text('colour="blue",'))) == "system.py:7:5: Machine.colour is not a setting."

    def test_unknown_name_gets_a_suggestion(self, workspace) -> None:
        assert one(workspace(machine_text("capabilities=[RemoteLogn()],"))) == (
            "system.py:7:19: `RemoteLogn` is not a Basix name. Did you mean `RemoteLogin`? "
            "The names are: Cache, Console, Desktop, Input, Machine, NixpkgsPin, Port, Ports, Program, RemoteLogin."
        )

    def test_unknown_import_gets_a_suggestion(self, workspace) -> None:
        text = machine_text().replace("import *", "import Machine, RemoteLogn")
        assert one(workspace(text)).startswith(
            "system.py:1:35: `RemoteLogn` is not a Basix name. Did you mean `RemoteLogin`?"
        )

    def test_interface_peer(self, workspace) -> None:
        assert one(workspace(machine_text('trusted_interfaces=["veth-t@if5"],'))) == (
            "system.py:7:25: Machine.trusted_interfaces[0] must not hold '@': "
            '`ip link` prints NAME@PEER; use only the part before @, found "veth-t@if5".'
        )

    def test_interface_alias(self, workspace) -> None:
        assert one(workspace(machine_text('trusted_interfaces=["eth0:1"],'))) == (
            "system.py:7:25: Machine.trusted_interfaces[0] must not hold ':': an alias label is not an interface, "
            'found "eth0:1".'
        )

    def test_interface_too_long(self, workspace) -> None:
        text = machine_text('trusted_interfaces=["enp1s0", "a-very-long-interface-name"],')
        assert one(workspace(text)) == (
            "system.py:7:35: Machine.trusted_interfaces[1] must be an interface name "
            "(1 to 15 letters, digits, '_', '.', or '-'), found \"a-very-long-interface-name\"."
        )

    def test_kernel_disk_name(self, workspace) -> None:
        text = machine_text().replace("/dev/disk/by-id/virtio-basix-disk", "/dev/sda")
        assert one(workspace(text)) == (
            "system.py:6:10: Machine.disk must not be a kernel name: kernel names move between boots. "
            'Use a path below /dev/disk/by-id/ (list them with `ls -l /dev/disk/by-id/`), found "/dev/sda".'
        )

    def test_positional_argument_gets_its_column(self, workspace) -> None:
        assert one(workspace(machine_text("user_ports=[Port(80)],"))) == (
            'system.py:7:17: Port must be 1024 or higher, or name its link: Port(80, interface="NAME").'
        )
        assert one(workspace(machine_text("programs=[pkgs.htop],"))) == (
            "system.py:7:15: Machine.programs[0] must be a program: Program(pkgs.NAME), found pkgs.htop."
        )

    def test_syntax_error(self, workspace) -> None:
        assert one(workspace(machine_text("capabilities=[RemoteLogin(],"))) == (
            "system.py:7:31: syntax error: closing parenthesis ']' does not match opening parenthesis '('."
        )

    def test_error_in_a_capability(self, workspace) -> None:
        text = (
            "from basix_config import *\n"
            "from nixdantic import Model\n"
            "from pynix_os.services.openssh import Openssh\n"
            "from basix_config.component import Capability\n\n"
            "class Ssh(Capability):\n"
            "    def emit(self, machine):\n"
            "        yield Openssh(enable=True, ports=[70000])\n\n"
            "machine = Machine(name='a', user='b', disk=None, capabilities=[Ssh()])\n"
        )
        assert one(workspace(text)) == (
            "system.py:8:43: Openssh.ports[0] must be 16 bit unsigned integer; between 0 and 65535 "
            "(both inclusive), found 70000. (NixOS option services.openssh.ports) (in Ssh)"
        )

    def test_no_machine(self, workspace) -> None:
        assert one(workspace("from basix_config import *\n")) == "system.py: the file must set `machine = Machine(...)`"

    def test_unreadable_file(self, tmp_path: Path) -> None:
        assert one(run_compile(tmp_path)) == "system.py:1:1: cannot read the file: No such file or directory."


class TestHelperErrors:
    """An error in a helper module names the helper file, then the line of `system.py` that led to it."""

    ENTRY = "from basix_config import *\nfrom helpers.mk import make\nmachine = make()\n"
    CAPABILITY = (
        "from basix_config import *\n"
        "from basix_config.component import Capability\n"
        "from pynix_os.services.openssh import Openssh\n\n"
        "class Ssh(Capability):\n"
        "    def emit(self, machine):\n"
        "        yield Openssh(enable=True, ports=[70000])\n"
    )

    def test_a_validation_error_names_the_helper(self, workspace) -> None:
        helper = "from basix_config import Machine\n\ndef make():\n    return Machine(name='a', user=42, disk=None)\n"
        assert one(workspace(self.ENTRY, {"helpers/mk.py": helper})) == (
            "helpers/mk.py:4:35: Machine.user must be an account name "
            "(a lower case letter, then up to 31 lower case letters, digits, or '-'), found 42. (from system.py:3)"
        )

    def test_the_real_command_names_the_helper(self, command) -> None:
        text = "from basix_config import *\nimport mymod\nmachine = Machine(name='a', user='b', disk=None)\n"
        assert one(command(text, {"mymod.py": "X = 1\nraise ValueError('boom')\n"})) == (
            "mymod.py:2: ValueError: boom (from system.py:2)"
        )

    def test_a_plain_error_names_the_helper(self, workspace) -> None:
        text = "from basix_config import *\nimport mymod\nmachine = Machine(name='a', user='b', disk=None)\n"
        assert one(workspace(text, {"mymod.py": "X = 1\nraise ValueError('boom')\n"})) == (
            "mymod.py:2: ValueError: boom (from system.py:2)"
        )

    def test_an_error_in_a_function_names_the_call_of_the_entry(self, workspace) -> None:
        text = "from basix_config import *\nimport mymod\n\nvalue = mymod.go()\nmachine = Machine(name='a', user='b', disk=None)\n"
        helper = "def go():\n    return 1 / 0\n"
        assert one(workspace(text, {"mymod.py": helper})) == (
            "mymod.py:2: ZeroDivisionError: division by zero (from system.py:4)"
        )

    def test_a_syntax_error_in_a_helper(self, workspace) -> None:
        text = (
            "from basix_config import *\nfrom helpers import mymod\nmachine = Machine(name='a', user='b', disk=None)\n"
        )
        compiled = workspace(text, {"helpers/mymod.py": "x = 1 +\n"})
        assert one(compiled) == "helpers/mymod.py:1:8: SyntaxError: invalid syntax (from system.py:2)"

    def test_a_wrong_name_in_a_helper_gets_no_basix_advice(self, workspace) -> None:
        text = "from basix_config import *\nimport mymod\nmachine = Machine(name='a', user='b', disk=None)\n"
        compiled = workspace(text, {"mymod.py": "from basix_config import RemoteLogn\n"})
        line = one(compiled)
        assert line.startswith("mymod.py:1: ImportError: cannot import name 'RemoteLogn'")
        assert line.endswith("(from system.py:2)")
        assert "Did you mean" not in line
        assert line == "mymod.py:1: ImportError: cannot import name 'RemoteLogn' from 'basix_config' (from system.py:2)"

    def test_a_missing_import_line_in_a_helper_names_the_helper(self, workspace) -> None:
        """The same text as minix: a Basix name that the helper does not import is "not imported"."""
        text = "from basix_config import *\nimport mymod\nmachine = Machine(name='a', user='b', disk=None)\n"
        assert one(workspace(text, {"mymod.py": "OWNER = pkgs.git\n"})) == (
            "mymod.py:1:9: `pkgs` is not imported. Add the line `from basix_config import *` at the top of "
            "mymod.py. (from system.py:2)"
        )

    def test_another_name_error_in_a_helper_names_the_helper(self, workspace) -> None:
        text = "from basix_config import *\nimport mymod\nmachine = Machine(name='a', user='b', disk=None)\n"
        assert one(workspace(text, {"mymod.py": "value = missing\n"})) == (
            "mymod.py:1:9: `missing` is not defined in mymod.py. The names you defined are: value. (from system.py:2)"
        )

    def test_a_failing_import_in_a_helper_names_the_helper(self, workspace) -> None:
        text = "from basix_config import *\nimport mymod\nmachine = Machine(name='a', user='b', disk=None)\n"
        assert one(workspace(text, {"mymod.py": "import nonesuch_module\n"})) == (
            "mymod.py:1: ModuleNotFoundError: No module named 'nonesuch_module' (from system.py:2)"
        )

    def test_a_missing_import_line_in_the_entry_is_not_imported(self, workspace) -> None:
        assert one(workspace("value = pkgs.git\n")) == (
            "system.py:1:9: `pkgs` is not imported. Add the line `from basix_config import *` at the top of system.py."
        )

    def test_another_name_error_in_the_entry_is_not_defined(self, workspace) -> None:
        assert one(workspace("from basix_config import *\nvalue = missing\n")) == (
            "system.py:2:9: `missing` is not defined in system.py. The names you defined are: value."
        )

    def test_the_entry_keeps_its_own_lines(self, workspace) -> None:
        text = "from basix_config import *\nimport mymod\nmachine = Machine(name='a', user=42, disk=None)\n"
        assert one(workspace(text, {"mymod.py": "X = 1\n"})).startswith("system.py:3:34: Machine.user must be")

    def test_a_capability_of_a_helper_names_the_helper(self, workspace) -> None:
        text = "from basix_config import *\nfrom helpers.ssh import Ssh\nmachine = Machine(name='a', user='b', disk=None, capabilities=[Ssh()])\n"
        assert one(workspace(text, {"helpers/ssh.py": self.CAPABILITY})) == (
            "helpers/ssh.py:7:43: Openssh.ports[0] must be 16 bit unsigned integer; between 0 and 65535 "
            "(both inclusive), found 70000. (NixOS option services.openssh.ports) (in Ssh) (from system.py:3)"
        )

    def test_the_code_of_an_input_package_is_named_below_the_package_folder(self, workspace) -> None:
        text = "from basix_config import *\nfrom comps import desk\nmachine = Machine(name='a', user='b', disk=None)\n"
        compiled = workspace(text, {".basix/pynix/comps/desk.py": "X = 1\nraise ValueError('no desk')\n"})
        assert one(compiled) == "comps/desk.py:2: ValueError: no desk (from system.py:2)"

    def test_a_generated_package_is_not_learner_code(self, workspace) -> None:
        text = "from basix_config import *\nimport pynix_gen\nmachine = Machine(name='a', user='b', disk=None)\n"
        compiled = workspace(text, {".basix/pynix/pynix_gen/__init__.py": "raise ValueError('generated')\n"})
        assert one(compiled) == "system.py:2: ValueError: generated"

    def test_a_site_in_the_json_is_a_path_below_the_workspace(self, workspace) -> None:
        helper = (
            "from basix_config import Machine, RemoteLogin\n\n"
            "def make():\n"
            "    return Machine(name='a', user='b', disk=None, capabilities=[RemoteLogin()])\n"
        )
        compiled = workspace(self.ENTRY, {"helpers/mk.py": helper})
        assert compiled.code == 0, compiled.err
        sources = {f["source"] for f in compiled.data["fragments"]}
        assert "helpers/mk.py:4 (Machine)" in sources
        assert "helpers/mk.py:4 (RemoteLogin)" in sources

    def test_a_site_in_the_entry_is_unchanged(self, workspace) -> None:
        compiled = workspace(machine_text("capabilities=[RemoteLogin()],"))
        assert compiled.code == 0, compiled.err
        sources = {f["source"] for f in compiled.data["fragments"]}
        assert "system.py:3 (Machine)" in sources
        assert "system.py:7 (RemoteLogin)" in sources


class TestPackages:
    """The name of a package is checked against the name list of the pinned nixpkgs (009-D19)."""

    def test_did_you_mean(self, workspace) -> None:
        line = one(workspace(machine_text("programs=[Program(pkgs.htpo)],")))
        assert line == "system.py:7:28: The package list has no package `htpo`. Did you mean `htop`?"

    def test_a_good_name_compiles(self, workspace) -> None:
        assert workspace(machine_text("programs=[Program(pkgs.htop)],")).code == 0


class TestNestedClass:
    """`options=` takes a root class only (009-D41)."""

    def test_nested_class_names_its_parent(self, workspace) -> None:
        text = machine_text("options=[nixos.Firewall_Interfaces_Value(allowedTCPPorts=[1])],")
        assert one(workspace(text)) == (
            "system.py:7:14: Machine.options[0] Firewall_Interfaces_Value is a part of Firewall; "
            'write Firewall(interfaces={"NAME": Firewall_Interfaces_Value(...)}).'
        )

    def test_a_root_class_compiles(self, workspace) -> None:
        compiled = workspace(machine_text("options=[nixos.Networking_Firewall(allowPing=False)],"))
        assert compiled.code == 0
        assert any(
            f["path"] == ["networking", "firewall"] and f["value"] == {"allowPing": False}
            for f in compiled.data["fragments"]
        )

    def test_a_name_that_the_namespace_lacks(self, workspace) -> None:
        line = one(workspace(machine_text("options=[nixos.Networking_Firewal(allowPing=False)],")))
        assert line.startswith("system.py:7:") and "Networking_Firewal" in line


class TestSecrets:
    """A secret never reaches the screen (SG39, SG43)."""

    def test_private_key_is_not_echoed(self, workspace) -> None:
        text = machine_text('capabilities=[RemoteLogin(authorized_keys=["-----BEGIN OPENSSH PRIVATE KEY-----"])],')
        line = one(workspace(text))
        assert line == (
            "system.py:7:48: RemoteLogin.authorized_keys[0] looks like a PRIVATE key. "
            "Never write it here. Give the public key, the line of the .pub file."
        )

    def test_secret_cache_key_is_not_echoed(self, workspace) -> None:
        secret = "example.cachix.org-1:" + "A" * 86 + "=="
        line = one(workspace(machine_text(f'caches=[Cache("https://example.cachix.org", "{secret}")],')))
        assert "AAAA" not in line and "looks like a SECRET key" in line


class TestCacheWarning:
    def test_http_gives_a_warning_and_exit_0(self, workspace) -> None:
        compiled = workspace(machine_text(f'caches=[Cache("http://example.cachix.org", "{KEY}")],'))
        assert compiled.code == 0
        assert compiled.lines == [
            "system.py:7:19: warning: Cache.url starts with http://, so the traffic is not encrypted. "
            "Nix still checks the signature of every file. Use http only on your own network."
        ]

    def test_a_cache_in_a_helper_names_the_helper(self, workspace) -> None:
        helper = f'from basix_config import Cache\nCACHE = Cache("http://example.cachix.org", "{KEY}")\n'
        text = "from basix_config import *\nfrom helpers.c import CACHE\nmachine = Machine(name='a', user='b', disk=None, caches=[CACHE])\n"
        compiled = workspace(text, {"helpers/c.py": helper})
        assert compiled.code == 0, compiled.err
        assert compiled.lines == [
            "helpers/c.py:2: warning: Cache.url starts with http://, so the traffic is not encrypted. "
            "Nix still checks the signature of every file. Use http only on your own network."
        ]


def report(*names: str) -> dict[str, str]:
    """A `facter.json` with the given interface names, in the form that `nixos-facter` writes."""
    items = [{"unix_device_names": [n], "sysfs_id": f"/class/net/{n}"} for n in names]
    return {"facter.json": json.dumps({"version": 1, "hardware": {"network_interface": items}})}


class TestInterfaceWarning:
    """BSX-369: a name that `facter.json` lacks gives one warning line. The exit code stays 0."""

    def test_a_name_in_the_report_gives_no_warning(self, workspace) -> None:
        text = machine_text('trusted_interfaces=["enp1s0"], user_ports=[Port(22, interface="wlan0")],')
        compiled = workspace(text, report("enp1s0", "wlan0", "lo"))
        assert compiled.code == 0 and compiled.err == ""

    def test_a_name_that_the_report_lacks_gives_a_warning_and_exit_0(self, workspace) -> None:
        text = machine_text('trusted_interfaces=["eno9"],')
        compiled = workspace(text, report("enp1s0", "wlan0"))
        assert compiled.code == 0
        assert compiled.lines == [
            "system.py:7:25: warning: the interface eno9 is not in facter.json; the machine has: enp1s0, wlan0"
        ]
        assert (compiled.folder / "system.json").is_file()

    def test_a_port_link_that_the_report_lacks_gives_a_warning(self, workspace) -> None:
        compiled = workspace(machine_text('user_ports=[Port(22, interface="tailscale0")],'), report("enp1s0"))
        assert compiled.code == 0
        assert compiled.lines == [
            "system.py:7:36: warning: the interface tailscale0 is not in facter.json; the machine has: enp1s0"
        ]

    def test_one_line_for_each_name_and_once(self, workspace) -> None:
        text = machine_text('trusted_interfaces=["a1", "a1", "enp1s0"], user_ports=[Port(22, interface="b2")],')
        lines = workspace(text, report("enp1s0")).lines
        assert [line.split(" is not")[0].split()[-1] for line in lines] == ["a1", "b2"]

    def test_the_real_command_warns_too(self, command) -> None:
        compiled = command(machine_text('trusted_interfaces=["eno9"],'), report("enp1s0"))
        assert compiled.code == 0
        assert compiled.lines == [
            "system.py:7:25: warning: the interface eno9 is not in facter.json; the machine has: enp1s0"
        ]

    def test_no_report_gives_no_warning(self, workspace) -> None:
        compiled = workspace(machine_text('trusted_interfaces=["eno9"], user_ports=[Port(22, interface="x")],'))
        assert compiled.code == 0 and compiled.err == ""

    def test_a_report_with_no_interface_list_gives_no_warning(self, workspace) -> None:
        for text in ("{}", "not json", '{"hardware": {"network_interface": []}}'):
            compiled = workspace(machine_text('trusted_interfaces=["eno9"],'), {"facter.json": text})
            assert compiled.code == 0 and compiled.err == "", text

    def test_the_report_does_not_enter_the_compiled_machine(self, workspace) -> None:
        text = machine_text('trusted_interfaces=["eno9"],')
        without = workspace(text)
        before = without.data
        (without.folder / "facter.json").write_text(report("enp1s0")["facter.json"])
        after = run_compile(without.folder)
        assert after.lines and after.data == before
        assert "facter.json" not in json.dumps(before["reads"])


class TestOutput:
    def test_the_json_holds_the_guard_and_the_reads(self, workspace) -> None:
        text = machine_text("capabilities=[RemoteLogin()],")
        compiled = workspace(text)
        assert compiled.code == 0 and compiled.err == ""
        data = compiled.data
        assert data["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
        assert data["reads"] == {"files": {}, "absent": [], "dirs": {}, "ignore": ["__pycache__", "system.json"]}
        assert sorted(data) == ["fragments", "modules", "name", "profile", "provenance", "reads", "source_sha256"]
        text_out = (compiled.folder / "system.json").read_text()
        assert text_out == json.dumps(data, indent=1, sort_keys=True) + "\n"

    def test_the_command_runs_the_entry_in_a_fixed_environment(self, command) -> None:
        """A real process: the child has a fixed hash seed and time zone, and no `PATH`."""
        env = '__import__("os").environ.get'
        compiled = command(
            machine_text(f'values={{"seed": {env}("PYTHONHASHSEED"), "tz": {env}("TZ"), "path": {env}("PATH")}},')
        )
        assert compiled.code == 0, compiled.err
        assert compiled.data["values"] == {"seed": "0", "tz": "UTC", "path": None}
        assert compiled.data["reads"]["ignore"] == ["__pycache__", "system.json"]

    def test_the_command_prints_one_line_for_an_error(self, command) -> None:
        compiled = command(machine_text("colour=1,"))
        assert one(compiled) == "system.py:7:5: Machine.colour is not a setting."
        assert not (compiled.folder / "system.json").exists()

    def test_the_command_names_its_usage(self) -> None:
        from basix_config.compile import main

        assert main(["compile"]) == 2

    def test_a_different_result_each_run_is_refused(self, workspace) -> None:
        """BSX-372: the entry runs twice and the results must match."""
        entry = machine_text('values={"n": __import__("random").random()},')
        assert one(workspace(entry)) == (
            "system.py:1:1: the file gives another result each time it runs. "
            "Remove the code that reads the clock, a random number, or the environment."
        )
        entry = machine_text('values={"t": __import__("time").time_ns()},')
        assert "another result each time" in one(workspace(entry))


class TestReadAudit:
    """009-D45: the compiler records every workspace file that the entry reads; a read outside is refused."""

    def test_a_read_file_gets_a_hash(self, workspace) -> None:
        text = machine_text('values={"note": (__import__("pathlib").Path("note.txt")).read_text()},')
        compiled = workspace(text, {"note.txt": "hello\n"})
        assert compiled.code == 0, compiled.err
        assert compiled.data["reads"]["files"] == {"note.txt": hashlib.sha256(b"hello\n").hexdigest()}
        assert compiled.data["values"] == {"note": "hello\n"}

    def test_a_folder_listing_is_recorded(self, workspace) -> None:
        text = machine_text('values={"names": sorted(p.name for p in __import__("pathlib").Path("dots").iterdir())},')
        compiled = workspace(text, {"dots/a.conf": "a", "dots/b.conf": "b"})
        assert compiled.code == 0, compiled.err
        assert compiled.data["reads"]["dirs"] == {"dots": ["a.conf", "b.conf"]}

    def test_a_missing_file_is_recorded_as_absent(self, workspace) -> None:
        text = (
            "from basix_config import *\n"
            "from pathlib import Path\n"
            "try:\n"
            "    extra = Path('local.txt').read_text()\n"
            "except FileNotFoundError:\n"
            "    extra = ''\n"
            "machine = Machine(name='a', user='b', disk=None, values={'extra': extra})\n"
        )
        assert workspace(text).data["reads"]["absent"] == ["local.txt"]

    def test_a_helper_module_of_the_workspace_is_recorded(self, workspace) -> None:
        text = "from basix_config import *\nimport helper\n\nmachine = Machine(name='a', user='b', disk=None, values={'v': helper.VALUE})\n"
        compiled = workspace(text, {"helper.py": "VALUE = 3\n"})
        assert compiled.code == 0, compiled.err
        assert compiled.data["values"] == {"v": 3}
        assert list(compiled.data["reads"]["files"]) == ["helper.py"]

    def test_a_helper_module_leaves_no_bytecode(self, command, tmp_path) -> None:
        text = "from basix_config import *\nimport helper\n\nmachine = Machine(name='a', user='b', disk=None, values={'v': helper.VALUE})\n"
        compiled = command(text, {"helper.py": "VALUE = 3\n"})
        assert compiled.code == 0, compiled.err
        assert compiled.data["values"] == {"v": 3}
        assert not list(tmp_path.rglob("__pycache__")) and not list(tmp_path.rglob("*.pyc"))

    def test_the_child_environment_writes_no_bytecode(self) -> None:
        from basix_config.compile import _child_env

        assert _child_env()["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_a_read_outside_the_workspace_is_refused(self, workspace, tmp_path_factory) -> None:
        outside = tmp_path_factory.mktemp("outside") / "secret.txt"
        outside.write_text("x")
        text = machine_text(f'values={{"x": open({str(outside)!r}).read()}},')
        assert one(workspace(text)) == (
            f"system.py:7:{text.splitlines()[6].index('open(') + 1}: reads {outside}, which is outside the workspace. "
            "Copy the file into the folder of system.py and read the copy."
        )

    def test_a_write_is_not_a_read(self, workspace) -> None:
        text = "from basix_config import *\nopen('out.txt', 'w').write('x')\nmachine = Machine(name='a', user='b', disk=None)\n"
        assert workspace(text).data["reads"]["files"] == {}


FOUND = """\
from basix_config import *

old = NixpkgsPin("old", "d233902339c02a9c334e7e593de68855ad26c4cb")

machine = Machine(
    name="trybox",
    user="learner",
    disk=None,
    inputs=[
        Input("nirinit", "github:amaanq/nirinit/0e43f61069f81bba1114475cae2e84aa0965a1a3"),
        Input("pytuin", "path:/somewhere/pytuin", nixpkgs=OWN),
        Input("nirinitold", "github:amaanq/nirinit/0e43f61069f81bba1114475cae2e84aa0965a1a3", nixpkgs=old),
    ],
)
"""
NIRINIT_HASH = "sha256-jpSYhiJNI5u/1CJ1RY+9BkSQtvishCfm5n+l9r7hdEk="
NIRINIT_REV = "0e43f61069f81bba1114475cae2e84aa0965a1a3"
PYTUIN_HASH = "sha256-I3a3onXGZDzPtXCAJmyUPDPcB9GYtaaSs/7HRpjkprU="
OLD_HASH = "sha256-30sZNZoA1cqF5JNO9fVX+wgiQYjB7HJqqJ4ztCDeBZE="
LAYER = "e7439b6b14ad3cc35d05608ebca9bce01a25f5f8"
OLD = "d233902339c02a9c334e7e593de68855ad26c4cb"
OWN_NIXPKGS = "18b9261cb3294b6d2a06d03f96872827b8fe2698"


def generation(folder: Path, module: str, record: dict) -> None:
    (folder / ".basix" / "pynix" / module).mkdir(parents=True, exist_ok=True)
    body = "".join(f"    {json.dumps(k)}: {json.dumps(v)},\n" for k, v in sorted(record.items()))
    (folder / ".basix" / "pynix" / module / "_generation.py").write_text(f"SOURCE: dict[str, str] = {{\n{body}}}\n")


def make_packages(folder: Path, nirinit_hash: str = NIRINIT_HASH) -> None:
    generation(
        folder, "pynix_nirinit", {"narHash": nirinit_hash, "rev": NIRINIT_REV, "nixpkgs": LAYER, "generator": "g1"}
    )
    generation(folder, "pynix_pytuin", {"narHash": PYTUIN_HASH, "nixpkgs": OWN_NIXPKGS, "generator": "g1"})
    generation(
        folder, "pynix_nirinitold", {"narHash": NIRINIT_HASH, "rev": NIRINIT_REV, "nixpkgs": OLD, "generator": "g1"}
    )
    generation(
        folder, "pynix_nixpkgs_old", {"commit": OLD, "narHash": OLD_HASH, "generator": "nixpkgs-names-1", "name": "old"}
    )


class TestInputs:
    """`Input(...)` and `NixpkgsPin(...)`: the syntax-tree read, the pair check, and the JSON (009-D48, 009-D49, 009-D53)."""

    def lock(self, folder: Path) -> None:
        shutil.copy(ROOT / "tests" / "cases" / "W3-flake.lock", folder / "flake.lock")

    def test_the_json_holds_the_inputs_and_the_package_hashes(self, tmp_path: Path) -> None:
        self.lock(tmp_path)
        make_packages(tmp_path)
        (tmp_path / "system.py").write_text(FOUND)
        compiled = run_compile(tmp_path)
        assert compiled.code == 0, compiled.err
        data = compiled.data
        assert [i["name"] for i in data["inputs"]] == ["nirinit", "pytuin", "nirinitold"]
        assert [i["source"] for i in data["inputs"]] == ["system.py:10", "system.py:11", "system.py:12"]
        assert data["inputs"][1]["nixpkgs"] == "own"
        assert data["inputs"][2]["nixpkgs"] == {"name": "old", "commit": OLD}
        assert "nixpkgs" not in data["inputs"][0]
        assert data["nixpkgs"] == [{"name": "old", "commit": OLD}]
        assert data["input_packages"] == {"nirinit": NIRINIT_HASH, "nirinitold": NIRINIT_HASH, "pytuin": PYTUIN_HASH}

    def test_a_stale_package_is_refused_at_the_line_of_the_input(self, tmp_path: Path) -> None:
        self.lock(tmp_path)
        make_packages(tmp_path, nirinit_hash="sha256-OTHER")
        (tmp_path / "system.py").write_text(FOUND)
        compiled = run_compile(tmp_path)
        assert compiled.code == 1
        assert compiled.lines == [
            "system.py:10: the package `pynix_nirinit` was made from another version of `nirinit` than flake.lock holds; "
            "run `basix apply` to make it again"
        ]

    def test_a_missing_package_and_an_unlocked_input(self, tmp_path: Path) -> None:
        (tmp_path / "system.py").write_text(FOUND)
        compiled = run_compile(tmp_path)  # no flake.lock yet
        assert compiled.code == 1
        assert compiled.lines[0] == "system.py:3: flake.lock holds no nixpkgs `old` yet; run `basix apply` to lock it"
        assert "system.py:10: flake.lock holds no input `nirinit` yet; run `basix apply` to lock it" in compiled.lines
        self.lock(tmp_path)
        compiled = run_compile(tmp_path)  # a lock, but no generated package
        assert compiled.lines[0] == "system.py:3: the package list `old` is not made yet; run `basix apply` to make it"
        assert (
            "system.py:10: the package `pynix_nirinit` is not made yet; run `basix apply` to make it" in compiled.lines
        )

    def test_a_computed_argument_is_refused_before_the_entry_runs(self, workspace) -> None:
        text = machine_text('inputs=[Input("a" + "b", "github:o/r")],')
        assert one(workspace(text)) == "system.py:7:13: Input(...) takes plain text and True or False only"

    def test_an_input_that_machine_does_not_list(self, workspace) -> None:
        text = (
            "from basix_config import *\n"
            'Input("lonely", "github:o/r", flake=False)\n'
            "machine = Machine(name='a', user='b', disk=None)\n"
        )
        compiled = workspace(text)  # `flake=False` has no package, so the pair check passes
        assert compiled.lines == [
            "system.py:2:1: the input `lonely` is not in `Machine(inputs=[...])`. Add it to the list."
        ]

    def test_an_input_built_in_a_helper_is_refused_at_the_machine_line(self, workspace) -> None:
        text = (
            "from basix_config import *\n"
            "from helper import hidden\n"
            "machine = Machine(name='a', user='b', disk=None, inputs=[hidden])\n"
        )
        compiled = workspace(
            text, {"helper.py": 'from basix_config import Input\nhidden = Input("hidden", "github:o/r", flake=False)\n'}
        )
        assert one(compiled) == (
            'system.py:3: the input `hidden` is not written as Input("hidden", "URL", ...) in system.py. '
            "Write the Input call in system.py with literal values."
        )

    def test_an_input_written_in_the_entry_is_kept(self, workspace) -> None:
        text = (
            "from basix_config import *\n"
            "machine = Machine(name='a', user='b', disk=None, inputs=[Input('src', 'github:o/r', flake=False)])\n"
        )
        compiled = workspace(text)
        assert compiled.code == 0, compiled.err
        assert [(i["name"], i["source"]) for i in compiled.data["inputs"]] == [("src", "system.py:2")]

    def test_an_input_with_no_flake_needs_no_package(self, tmp_path: Path) -> None:
        text = machine_text('inputs=[Input("src", "github:o/r", flake=False)],')
        (tmp_path / "system.py").write_text(text)
        shutil.copy(ROOT / "tests" / "cases" / "W3-flake.lock", tmp_path / "flake.lock")
        compiled = run_compile(tmp_path)
        # The input is not in this lock, but a plain source folder has no package to compare.
        assert compiled.code == 0, compiled.err
        assert "input_packages" not in compiled.data


COMPS_HASH = "sha256-COMPSCOMPSCOMPSCOMPSCOMPSCOMPSCOMPSCOMPSCOM="

COMPS_ENTRY = """\
from comps import zone

from basix_config import *

machine = Machine(
    name="trybox",
    user="learner",
    disk=None,
    inputs=[Input("comps", "path:/somewhere/comps", python="src"%(flake)s)],
    values={"zone": zone()},
)
"""


class TestPythonInput:
    """`Input(python="src")`: the entry imports the Python code of an input from `.basix/pynix`."""

    @staticmethod
    def record(narhash: str = COMPS_HASH) -> dict[str, str]:
        """The `_generation.py` record of `pynix_comps`. An input that is not a flake has one too."""
        return {"narHash": narhash, "generator": "g1", "nixpkgs": LAYER}

    def make(self, folder: Path, code: str, *, flake: bool = True, record: bool = True) -> None:
        """The workspace that `basix apply` leaves: a lock, the package record, and links to the code of the input."""
        lock = json.loads((ROOT / "tests" / "cases" / "W3-flake.lock").read_text())
        lock["nodes"]["comps"] = {
            "locked": {"type": "path", "path": "/somewhere/comps", "narHash": COMPS_HASH, "lastModified": 1},
            "original": {"type": "path", "path": "/somewhere/comps"},
        }
        lock["nodes"]["root"]["inputs"]["comps"] = "comps"
        (folder / "flake.lock").write_text(json.dumps(lock))
        if record:
            generation(folder, "pynix_comps", self.record())
        # The code is outside the workspace, as in the Nix store. The link in `.basix/pynix` is as `nix build` makes it.
        (folder / ".basix" / "pynix").mkdir(parents=True, exist_ok=True)
        store = folder.parent / (folder.name + "-store") / "src" / "comps"
        store.mkdir(parents=True)
        (store / "__init__.py").write_text(code)
        (folder / ".basix" / "pynix" / "comps").symlink_to(store, target_is_directory=True)
        (folder / "system.py").write_text(COMPS_ENTRY % {"flake": "" if flake else ", flake=False"})

    def test_the_entry_imports_the_code_of_the_input(self, tmp_path: Path) -> None:
        self.make(tmp_path, "def zone():\n    return 'Europe/Oslo'\n")
        compiled = run_compile(tmp_path)
        assert compiled.code == 0, compiled.err
        data = compiled.data
        assert data["values"] == {"zone": "Europe/Oslo"}
        assert data["reads"]["files"] == {}
        assert [(i["name"], i["python"]) for i in data["inputs"]] == [("comps", "src")]
        assert data["input_packages"] == {"comps": COMPS_HASH}

    def test_an_input_with_no_flake_has_its_record_too(self, tmp_path: Path) -> None:
        self.make(tmp_path, "def zone():\n    return 'UTC'\n", flake=False)
        compiled = run_compile(tmp_path)
        assert compiled.code == 0, compiled.err
        assert compiled.data["input_packages"] == {"comps": COMPS_HASH}

    def test_an_input_with_no_python_has_no_record(self, workspace) -> None:
        text = machine_text('inputs=[Input("src", "github:o/r", flake=False)],')
        assert "input_packages" not in workspace(text).data

    def test_a_moved_lock_is_refused_at_the_line_of_the_input(self, tmp_path: Path) -> None:
        self.make(tmp_path, "def zone():\n    return 'UTC'\n")
        generation(tmp_path, "pynix_comps", self.record("sha256-OTHER"))
        assert one(run_compile(tmp_path)) == (
            "system.py:9: the package `pynix_comps` was made from another version of `comps` than flake.lock holds; "
            "run `basix apply` to make it again"
        )

    def test_a_missing_record_is_refused(self, tmp_path: Path) -> None:
        self.make(tmp_path, "def zone():\n    return 'UTC'\n", flake=False, record=False)
        assert one(run_compile(tmp_path)) == (
            "system.py:9: the package `pynix_comps` is not made yet; run `basix apply` to make it"
        )

    def test_an_error_in_the_code_names_its_file_and_the_line_of_the_entry(self, tmp_path: Path) -> None:
        self.make(tmp_path, "def zone():\n    return 1 / 0\n")
        (tmp_path / "system.py").write_text(COMPS_ENTRY.replace("zone()", "zone() + ''") % {"flake": ""})
        assert one(run_compile(tmp_path)) == (
            "comps/__init__.py:2: ZeroDivisionError: division by zero (from system.py:10)"
        )

    def test_a_capability_made_in_the_code_names_its_file_as_the_source(self, tmp_path: Path) -> None:
        """The link in `.basix/pynix` points outside the workspace, as in the Nix store: the name keeps `comps/`."""
        code = (
            "from basix_config.component import Capability\n"
            "from pynix_os.time.__options__ import Time\n\n"
            "def zone():\n    return 'UTC'\n\n"
            "class Clock(Capability):\n"
            "    def emit(self, machine):\n"
            "        yield Time(timeZone='UTC')\n\n"
            "CLOCK = Clock()\n"
        )
        self.make(tmp_path, code)
        text = COMPS_ENTRY.replace("import zone", "import CLOCK, zone").replace(
            "    values=", "    capabilities=[CLOCK],\n    values="
        )
        (tmp_path / "system.py").write_text(text % {"flake": ""})
        compiled = run_compile(tmp_path)
        assert compiled.code == 0, compiled.err
        sources = {f["source"] for f in compiled.data["fragments"] if f["path"] == ["time"]}
        assert sources == {"comps/__init__.py:11 (Clock)"}, sources

    def test_the_real_command_imports_the_code(self, tmp_path: Path) -> None:
        self.make(tmp_path, "def zone():\n    return 'Europe/Oslo'\n")
        compiled = run_command(tmp_path)
        assert compiled.code == 0, compiled.err
        assert compiled.data["values"] == {"zone": "Europe/Oslo"}
        assert compiled.data["reads"]["files"] == {}
        assert not list(tmp_path.rglob("__pycache__")) and not list(tmp_path.parent.rglob("*-store/**/*.pyc"))


class TestNixpkgsNames:
    """`old.pkgs.NAME` is checked against the name list that apply made (009-D54)."""

    def make(self, folder: Path) -> None:
        shutil.copy(ROOT / "tests" / "cases" / "W3-flake.lock", folder / "flake.lock")
        generation(
            folder,
            "pynix_nixpkgs_old",
            {"commit": OLD, "narHash": OLD_HASH, "generator": "nixpkgs-names-1", "name": "old"},
        )
        package = folder / ".basix" / "pynix" / "pynix_nixpkgs_old"
        (package / "__init__.py").write_text("")
        (package / "names.json").write_text(json.dumps({"hello": ["hello", 1], "htop": ["htop", 1]}))

    ENTRY = """\
from basix_config import *

old = NixpkgsPin("old", "d233902339c02a9c334e7e593de68855ad26c4cb")

machine = Machine(
    name="trybox",
    user="learner",
    disk=None,
    programs=[Program(old.pkgs.NAME)],
)
"""

    def test_a_good_name_becomes_a_reference_to_that_nixpkgs(self, tmp_path: Path) -> None:
        self.make(tmp_path)
        (tmp_path / "system.py").write_text(self.ENTRY.replace("NAME", "hello"))
        compiled = run_compile(tmp_path)
        assert compiled.code == 0, compiled.err
        packages = next(f for f in compiled.data["fragments"] if f["path"] == ["environment"])["value"][
            "systemPackages"
        ]
        assert packages[0]["__nixdantic_ref__"] == {"kind": "nixpkgs_package", "attr": "hello", "nixpkgs": "old"}
        assert compiled.data["nixpkgs"] == [{"name": "old", "commit": OLD}]

    def test_a_wrong_name_gets_a_suggestion(self, tmp_path: Path) -> None:
        self.make(tmp_path)
        (tmp_path / "system.py").write_text(self.ENTRY.replace("NAME", "helo"))
        assert one(run_compile(tmp_path)) == (
            "system.py:9:32: The package list `old` has no package `helo`. Did you mean `hello`?"
        )


class TestAdvancedImport:
    """L128, BSX-396: an advanced file imports `Force`, `Before`, `After`, and `Order` from `nixdantic`."""

    ENTRY = """\
from nixdantic import After, Before, Default, Force, Order
from pynix_os.networking.__options__ import Networking
from pynix_os.time.__options__ import Time

from basix_config import *

machine = Machine(
    name="pybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-basix-disk",
    options=[
        {options}
    ],
)
"""

    def entry(self, options: str) -> str:
        return self.ENTRY.format(options=options)

    def test_the_wrappers_compile_to_priority_and_order(self, workspace) -> None:
        compiled = workspace(
            self.entry(
                'Time(timeZone=Force("UTC")),'
                ' Networking(nameservers=Before(["1.1.1.1"])),'
                ' Networking(nameservers=After(["8.8.8.8"])),'
                ' Networking(nameservers=Order(700, ["9.9.9.9"])),'
                ' Networking(domain=Default("home.arpa")),'
            )
        )
        assert compiled.code == 0, compiled.err
        text = "".join(json.dumps(f["value"], sort_keys=True) for f in compiled.data["fragments"])

        def marked(kind: str, level: int, value: object) -> str:
            return json.dumps({"__nixdantic_ref__": {"kind": kind, "level": level, "value": value}}, sort_keys=True)

        assert marked("priority", 50, "UTC") in text
        assert marked("order", 500, ["1.1.1.1"]) in text
        assert marked("order", 1500, ["8.8.8.8"]) in text
        assert marked("order", 700, ["9.9.9.9"]) in text
        assert marked("priority", 1000, "home.arpa") in text

    def test_the_star_import_does_not_give_a_wrapper(self, workspace) -> None:
        text = machine_text('values={"zone": Force("UTC")},')
        line = one(workspace(text))
        assert line.startswith("system.py:7:") and "`Force` is not a Basix name" in line

    def test_a_wrapper_inside_a_wrapper_is_one_line(self, workspace) -> None:
        text = self.entry('Time(timeZone=Force(Before("UTC"))),')
        assert one(workspace(text)) == (
            "system.py:12:23: Force(...) cannot hold Before(...): use one wrapper for one value."
        )

    def test_a_wrapper_on_one_list_item_is_one_line(self, workspace) -> None:
        text = self.entry('Networking(nameservers=[Before("1.1.1.1")]),')
        assert one(workspace(text)) == (
            "system.py:12:33: Before(...) works on a whole value, not on one item of a list."
        )

    def test_the_inner_value_gets_the_normal_check(self, workspace) -> None:
        line = one(workspace(self.entry("Time(timeZone=Force(5)),")))
        assert line.startswith("system.py:12:") and "found 5" in line, line


class TestOptionLines:
    """`BSX-385`: each item of `options=[...]` carries its own line, so a conflict names both lines."""

    ENTRY = """\
from pynix_os.networking.__options__ import Networking
from pynix_os.time.__options__ import Time

from basix_config import *

machine = Machine(
    name="pybox",
    user="learner",
    disk="/dev/disk/by-id/virtio-basix-disk",
    options=[
        Time(timeZone="UTC"),
        Networking(domain="a.example"),
        Networking(
            domain="b.example",
        ),
    ],
)
"""

    def test_each_option_has_its_own_line(self, workspace) -> None:
        compiled = workspace(self.ENTRY)
        assert compiled.code == 0, compiled.err
        sources = {tuple(f["path"]): f["source"] for f in compiled.data["fragments"] if f["path"] in (["time"],)}
        assert sources == {("time",): "system.py:11 (Machine)"}
        domains = [
            f["source"] for f in compiled.data["fragments"] if f["path"] == ["networking"] and "domain" in f["value"]
        ]
        assert domains == ["system.py:12 (Machine)", "system.py:13 (Machine)"]
        machine = [f["source"] for f in compiled.data["fragments"] if f["path"] == ["basix"]]
        assert machine == ["system.py:6 (Machine)"]  # the other values keep the line of `Machine(...)`

    def test_a_list_that_is_not_written_in_place_keeps_the_line_of_machine(self, workspace) -> None:
        text = self.ENTRY.replace("    options=[", "    options=list([").replace("    ],\n)", "    ]),\n)")
        compiled = workspace(text)
        assert compiled.code == 0, compiled.err
        sources = {f["source"] for f in compiled.data["fragments"] if f["path"] == ["time"]}
        assert sources == {"system.py:6 (Machine)"}
