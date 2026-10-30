"""Compile a machine file: `python -m basix_config.compile system.py system.json`.

The compile step runs before the build, and Python never runs inside Nix evaluation (CONCEPT 5). The parent process:

1. reads the `Input(...)` and `NixpkgsPin(...)` calls from the syntax tree, before the entry runs (009-D49);
2. checks that each generated input package comes from the input that `flake.lock` holds (the pair check, 009-D48);
3. runs the entry in a child process with a fixed environment and hash seed, twice, and compares the two results
   (BSX-372). A different result means that the entry reads the clock, a random number, or the environment;
4. writes the JSON.

The child runs the entry under the read audit (009-D45). The JSON holds `"source_sha256"`, the SHA-256 (hex) of the
bytes of the entry. Nix computes the same value with `builtins.hashFile "sha256" ./system.py`, and `lib.machine`
throws when the two differ (the file changed after the last `basix apply`). The JSON also holds `"reads"`: one hash for
each workspace file that the entry reads.

An error prints one plain line that starts with `FILE:LINE:COLUMN:` and exits 1. Pydantic writes the text of a
validation error (`nixdantic.Plain`, `Expect`, and the validators of `basix_config`). This file finds the place in the
source and prints the line. It prints no Python traceback.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import runpy
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, NamedTuple

from pydantic import BaseModel, ValidationError
from pynix_native import (
    DiscoveryError,
    Found,
    check_pairs,
    discover_source,
    error_lines,
    read_source,
    set_apply_command,
)
from pynix_native.inputs import input_package_name

from basix_config.component import learner_path, set_workspace

#: The command that makes the generated packages. The pair check and the package errors name it.
COMMAND = "basix apply"
#: The environment of the child. It is fixed, so the same entry gives the same result on every machine.
FIXED_ENV = {
    "PYTHONHASHSEED": "0",
    "PYTHONUTF8": "1",
    "PYTHONNOUSERSITE": "1",
    # The child writes no `__pycache__` next to a helper module of the workspace. A `.pyc` file would enter the save.
    "PYTHONDONTWRITEBYTECODE": "1",
    "LC_ALL": "C.UTF-8",
    "LANG": "C.UTF-8",
    "TZ": "UTC",
}
#: The variables that the child takes from the parent: the library path only. `PATH` is not one of them.
PASS_ENV = ("PYTHONPATH",)
CHILD_TIMEOUT_S = 120
#: The names of the priority and order wrappers (L128). A `PriorityError` points at one of these calls.
WRAPPERS = frozenset({"Force", "Default", "Before", "After", "Order"})


def _place(tree: ast.Module, line: int | None, loc: tuple[object, ...], kind: str) -> tuple[int, int]:
    """Line and column (from 1) of the wrong value, or of the wrong keyword for an unknown name."""
    calls = (
        [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and line is not None
            and n.lineno <= line <= (n.end_lineno or n.lineno)
            and any(k.arg == loc[0] for k in n.keywords or [])
        ]
        if loc
        else []
    )
    if not calls:  # a missing setting, or no match: the call itself
        call = next((n for n in ast.walk(tree) if isinstance(n, ast.Call) and n.lineno == line), None)
        return (call.lineno, call.col_offset + 1) if call else (line or 1, 1)
    keyword = next(k for k in max(calls, key=lambda n: n.lineno).keywords if k.arg == loc[0])
    node: ast.AST = keyword if kind == "extra_forbidden" else keyword.value
    # Go into a list or a call: `ports[0]`, `settings=X(AllowUsers=...)`.
    for part in loc[1:] if kind != "extra_forbidden" else ():
        if isinstance(part, int) and isinstance(node, ast.List | ast.Tuple) and part < len(node.elts):
            node = node.elts[part]
        elif isinstance(node, ast.Call):
            node = next((k.value for k in node.keywords if k.arg == part), node)
    return getattr(node, "lineno", line or 1), getattr(node, "col_offset", 0) + 1


def _positional(tree: ast.Module, line: int | None, title: str, loc: tuple[object, ...]) -> ast.expr | None:
    """The argument that the learner wrote by position, as in `Port(80)` or `Program(pkgs.htop)`."""
    import basix_config

    fields = list(getattr(getattr(basix_config, title, None), "model_fields", {}))
    if line is None or not loc or loc[0] not in fields:
        return None
    index = fields.index(str(loc[0]))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == title
            and node.lineno <= line <= (node.end_lineno or node.lineno)
            and index < len(node.args)
        ):
            return node.args[index]
    return None


def _given(value: object) -> str:
    if type(value).__name__ in {"Pkg", "Prog"}:  # a package that stands alone, as in `programs=[pkgs.htop]`
        return f"pkgs.{getattr(value, 'attr', '?')}"
    text = json.dumps(value) if isinstance(value, str | int | float | bool | list | dict | type(None)) else repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


def _path(loc: tuple[object, ...]) -> str:
    return "".join(f"[{p}]" if isinstance(p, int) else (f".{p}" if i else str(p)) for i, p in enumerate(loc))


def _option(ctx: dict[str, Any]) -> str:
    """The NixOS option of the field, where a generated root class knows it (008-Q16)."""
    return f" (NixOS option {ctx['option']})" if ctx.get("option") else ""


#: Error types that must not echo the value: a secret key must never reach a terminal or a log.
_NO_FOUND = {"missing", "extra_forbidden", "nested_class", "key_private", "cache_secret", "cache_duplicate"}


def _validation(name: str, tree: ast.Module, line: int | None, error: ValidationError, where: str = "") -> list[str]:
    lines = []
    for item in error.errors(include_url=False):
        ctx, kind, loc = dict(item.get("ctx") or {}), item["type"], tuple(item["loc"])
        row, col = _place(
            tree, line, (*loc, ctx["item"]) if "item" in ctx else loc, kind
        )  # a rule can name one list item
        arg = _positional(tree, line, error.title, loc) if kind != "extra_forbidden" else None
        if arg is not None:
            row, col = arg.lineno, arg.col_offset + 1
        found = "" if kind in _NO_FOUND or not loc else f", found {_given(item['input'])}"
        hint = " ".join(str(ctx.get("hint", "")).split())  # the help of the field, from its docstring
        text = f"{item['msg']}{found}"
        text += "" if text.endswith((".", "?")) else "."
        path = "." + _path(loc) if loc else ""
        lines.append(f"{name}:{row}:{col}: {error.title}{path} {text}{' ' + hint if hint else ''}{_option(ctx)}{where}")
    return lines


def _unknown_name(name: str, tree: ast.Module, line: int | None, missing: str) -> list[str]:
    import basix_config

    hidden = {"ComponentError", "NativeModule", "ConfigRef", "Native", "Package", "Text", "OWN"}
    known = sorted(n for n in basix_config.__all__ if n[:1].isupper() and n not in hidden)
    close = difflib.get_close_matches(missing, known, n=1)
    spot = next(
        (
            n
            for n in ast.walk(tree)
            if getattr(n, "lineno", None) == line and missing in (getattr(n, "id", None), getattr(n, "name", None))
        ),
        None,
    )
    advice = f" Did you mean `{close[0]}`?" if close else ""
    col = getattr(spot, "col_offset", 0) + 1
    return [f"{name}:{line or 1}:{col}: `{missing}` is not a Basix name.{advice} The names are: {', '.join(known)}."]


def _source_line(error: BaseException, name: str) -> int | None:
    frames = [f for f in traceback.extract_tb(error.__traceback__) if Path(f.filename).name == name]
    return frames[-1].lineno if frames else None


class Spot(NamedTuple):
    """Where an error happened, in the deepest file of the learner."""

    name: str  # the file name in the plain line: `system.py`, or `helpers/desk.py` for a helper module
    tree: ast.Module  # the syntax tree of that file
    line: int | None
    entry: bool  # the file is the entry
    origin: str  # ` (from system.py:7)` for a helper module, else empty


def _from(name: str, line: int | None) -> str:
    """The tail of a plain line for a file that is not the entry: the line of the entry that led to it."""
    return f" (from {name}:{line})" if line else ""


def _spot(error: BaseException, source: Path, tree: ast.Module, fallback: int | None = None) -> Spot:
    """The deepest file of the learner in the traceback of `error`.

    A helper module is a file below the workspace folder, or below `.basix/pynix` (the code of an input package).
    When the deepest file is the entry, the line is the last line of the entry in the traceback. When it is a helper,
    the line is the last line of that helper, and the tail names the last line of the entry (or `fallback`).
    """
    name, root = source.name, source.resolve().parent
    deepest: tuple[str, Path, int | None] | None = None
    entry_line: int | None = None
    for frame in traceback.extract_tb(error.__traceback__):
        path = Path(os.path.abspath(frame.filename))
        if path.name == name:
            deepest, entry_line = (name, source, frame.lineno), frame.lineno
        elif (shown := learner_path(path, root)) is not None:
            deepest = (shown, path, frame.lineno)
    if deepest is None or deepest[0] == name:
        return Spot(name, tree, deepest[2] if deepest else None, True, "")
    try:
        helper = ast.parse(deepest[1].read_text())
    except (OSError, SyntaxError, ValueError):  # the helper changed or does not parse: keep the line of the entry
        return Spot(name, tree, entry_line, True, "")
    return Spot(deepest[0], helper, deepest[2], False, _from(name, entry_line or fallback))


def _syntax(name: str, error: SyntaxError) -> list[str]:
    """One plain line for a Python syntax error. No traceback."""
    text = " ".join(str(error.msg).split())
    text += "" if text.endswith((".", "?")) else "."
    return [f"{name}:{error.lineno or 1}:{error.offset or 1}: syntax error: {text}"]


def _helper_name(filename: object, source: Path) -> str | None:
    """The name of a helper module of the learner (not the entry), or `None` for any other file."""
    if not filename:
        return None
    path = Path(os.path.abspath(str(filename)))
    return None if path.name == source.name else learner_path(path, source.resolve().parent)


def report(source: Path, error: BaseException) -> list[str]:
    """The plain lines for an error of the entry."""
    from nixdantic import PriorityError
    from pynix_pkgs import PackageError

    from basix_config import ComponentError

    name = source.name
    if (
        isinstance(error, SyntaxError) and Path(str(error.filename or "")).name == name
    ):  # the file itself does not parse
        return _syntax(name, error)
    if isinstance(error, OSError) and Path(str(error.filename or "")).name == name:
        return [f"{name}:1:1: cannot read the file: {error.strerror}."]
    if isinstance(error, SyntaxError) and (shown := _helper_name(error.filename, source)):  # a helper does not parse
        text = " ".join(str(error.msg).split())
        return [
            f"{shown}:{error.lineno or 1}:{error.offset or 1}: SyntaxError: {text}{_from(name, _source_line(error, name))}"
        ]
    try:
        tree = ast.parse(source.read_text())
    except (
        SyntaxError,
        ValueError,
        UnicodeDecodeError,
    ) as inner:  # an error of another kind, and the file does not parse
        return (
            _syntax(name, inner) if isinstance(inner, SyntaxError) else [f"{name}:1:1: cannot read the file: {inner}."]
        )
    if isinstance(
        error, ComponentError
    ):  # a capability failed inside `emit`: the traceback of the inner error has the line
        inner = error.error
        if isinstance(inner, ValidationError):
            where = error.where.rpartition(":")
            spot = _spot(inner, source, tree, int(where[2]) if where[0] == name and where[2].isdigit() else None)
            lines = _validation(spot.name, spot.tree, spot.line, inner, f" (in {error.label})")
            return [line + spot.origin for line in lines]
        return [f"{error.where}: the capability {error.label} failed: {type(inner).__name__}: {inner}"]
    spot = _spot(error, source, tree)
    display, tree, line = spot.name, spot.tree, spot.line  # the deepest file: the entry, else a helper module
    if isinstance(error, ValidationError):
        return [text + spot.origin for text in _validation(display, tree, line, error)]
    if isinstance(error, ImportError) and "cannot import name" in str(error) and spot.entry:
        return _unknown_name(name, tree, line, str(error).split("'")[1])
    if isinstance(error, PackageError):  # a package name that nixpkgs does not have, with "did you mean" (009-D19)
        spot_node = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.lineno == line and n.attr == error.word),
            None,
        )
        const = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.lineno == line and n.value == error.word),
            None,
        )
        if isinstance(spot_node, ast.Attribute) and spot_node.end_col_offset:
            col = spot_node.end_col_offset - len(error.word) + 1
        elif const is not None:
            col = const.col_offset + 2
        else:
            col = 1
        return [f"{display}:{line or 1}:{col}: {error}{spot.origin}"]
    if isinstance(error, AttributeError) and " has no class " in str(
        error
    ):  # a name of the `nixos` namespace that has no class
        word = str(error).split(" has no class ")[1].split(".")[0]
        node = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.lineno == line and n.attr == word), None
        )
        col = (node.end_col_offset - len(word) + 1) if node and node.end_col_offset else 1
        return [f"{display}:{line or 1}:{col}: {error}{spot.origin}"]
    if isinstance(error, PriorityError):  # a wrapper of the advanced import (L128) used where it has no meaning
        call = next(
            (
                n
                for n in ast.walk(tree)
                if isinstance(n, ast.Call) and n.lineno == line and getattr(n.func, "id", "") in WRAPPERS
            ),
            None,
        )
        return [f"{display}:{line or 1}:{call.col_offset + 1 if call else 1}: {error}{spot.origin}"]
    if isinstance(error, NameError) and error.name and spot.entry:
        return _unknown_name(name, tree, line, error.name)
    return [f"{display}:{line or 1}: {type(error).__name__}: {error}{spot.origin}"]


def _warnings(source: Path) -> None:
    """Print one warning line for each cache warning. The exit code stays 0."""
    from basix_config import cache

    if not cache.WARNINGS:
        return
    tree = ast.parse(source.read_text())
    for where, index, keyword, text, value in cache.WARNINGS:
        file, _, number = where.rpartition(":")
        line = int(number)
        if file != source.name:  # a `Cache` built in a helper module: its line is a line of that file
            print(f"{file}:{line}: warning: Cache.{keyword} {text}", file=sys.stderr)
            continue
        calls = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and getattr(n.func, "id", None) == "Cache"
            and n.lineno <= line <= (n.end_lineno or n.lineno)
        ]
        args = [
            c.args[index] if index < len(c.args) else next((k.value for k in c.keywords if k.arg == keyword), None)
            for c in calls
        ]
        arg = next((a for a in args if isinstance(a, ast.Constant) and a.value == value), args[0] if args else None)
        row, col = (arg.lineno, arg.col_offset + 1) if arg else (line, 1)
        print(f"{source.name}:{row}:{col}: warning: Cache.{keyword} {text}", file=sys.stderr)


def _report_names(folder: Path) -> set[str]:
    """The interface names of the hardware report `facter.json` in the workspace.

    The set is empty when there is no report, when the report is not valid JSON, or when it lists no interface.
    """
    try:
        items = json.loads((folder / "facter.json").read_text())["hardware"]["network_interface"]
        names: set[str] = set()
        for item in items:
            names.update(item.get("unix_device_names") or [])
            names.add(item.get("unix_device_name"))
        return {n for n in names if isinstance(n, str) and n}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return set()


def _interface_warnings(source: Path, machine: BaseModel) -> None:
    """Print one warning line for each interface name that `facter.json` lacks. The exit code stays 0 (BSX-369).

    No report, an unreadable report, or a report with no interface gives no warning. The file is read here, after the
    read audit, so it does not enter `system.json`: a new report never makes the compiled machine stale.
    """
    from basix_config.fields import interface_names

    have = _report_names(source.resolve().parent)
    if not have:
        return
    tree = ast.parse(source.read_text())
    for name in interface_names(machine):
        if name in have:
            continue
        spot = next((n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == name), None)
        row, col = (spot.lineno, spot.col_offset + 1) if spot else (1, 1)
        print(
            f"{source.name}:{row}:{col}: warning: the interface {name} is not in facter.json; "
            f"the machine has: {', '.join(sorted(have))}",
            file=sys.stderr,
        )


def run_entry(source: Path, out: Path, final: Path) -> int:
    """The child: run the entry under the read audit and write the compiled JSON to `out`."""
    try:
        return _run_entry(source, out, final)
    finally:
        set_workspace(None)


def _run_entry(source: Path, out: Path, final: Path) -> int:
    """The child: run the entry under the read audit and write the compiled JSON to `out`.

    `final` is the path of the JSON that the parent writes at the end. The audit skips its name in a folder listing.
    """
    from nixdantic.reads import Reads

    root = source.resolve().parent
    set_apply_command(COMMAND)
    set_workspace(root)  # a site is a path below this folder, so an error in a helper module names its file
    sys.path.append(str(root / ".basix" / "pynix"))  # the generated input packages (009-D47)
    sys.path.append(str(root))  # a helper module of the workspace; the libraries come first
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()  # the bytes that the run reads
        with Reads(source, final) as reads:
            scope = runpy.run_path(str(source), run_name="__basix__")
            from basix_config import Machine

            machine = scope.get("machine")
            if not isinstance(machine, Machine):
                print(f"{source.name}: the file must set `machine = Machine(...)`", file=sys.stderr)
                return 1
            compiled = machine.compile()
    except Exception as error:  # every learner error becomes plain lines
        for text in report(source, error):
            print(text, file=sys.stderr)
        return 1
    if lines := reads.refusals():  # a read outside the workspace
        print("\n".join(lines), file=sys.stderr)
        return 1
    _warnings(source)
    _interface_warnings(source, machine)
    compiled["source_sha256"] = digest
    compiled["reads"] = reads.result()
    out.write_text(json.dumps(compiled, indent=1, sort_keys=True) + "\n")
    return 0


def _child_env() -> dict[str, str]:
    env = dict(FIXED_ENV)
    env.update({k: os.environ[k] for k in PASS_ENV if k in os.environ})
    env.update({k: v for k, v in os.environ.items() if k.startswith("NIX_PYTHON")})  # the library path of a Nix Python
    return env


def _run_child(source: Path, final: Path, tmp: Path, number: int) -> tuple[int, str, bytes]:
    """One run of the entry in a child process. Returns the exit code, the text of stderr, and the JSON bytes."""
    out = tmp / f"run{number}.json"
    command = [sys.executable, "-P", "-m", "basix_config.compile", "--run", str(source), str(out), str(final)]
    done = subprocess.run(
        command,
        cwd=source.resolve().parent,
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )
    return done.returncode, done.stderr, out.read_bytes() if out.exists() else b""


def _discover(source: Path) -> tuple[Found | None, list[str]]:
    """The `Input(...)` and `NixpkgsPin(...)` calls of the entry, read from its syntax tree."""
    text = source.read_text()
    try:
        ast.parse(text, source.name)
    except SyntaxError as error:
        return None, _syntax(source.name, error)
    try:
        return discover_source(text, source.name), []
    except DiscoveryError as error:
        return None, [str(error)]


def _pair_check(found: Found, workspace: Path) -> tuple[list[str], dict[str, str]]:
    """The pair check (009-D48). Returns the plain error lines and the narHash of each input package."""
    lock_file = workspace / "flake.lock"
    lock = json.loads(lock_file.read_text()) if lock_file.exists() else {"root": "root", "nodes": {"root": {}}}
    packages = workspace / ".basix" / "pynix"
    lines = error_lines(check_pairs(found, lock, packages, COMMAND))
    narhash: dict[str, str] = {}
    if not lines:
        for item in found.inputs:
            record = read_source(packages / input_package_name(item.name) / "_generation.py") if item.flake else None
            if record and record.get("narHash"):
                narhash[item.name] = record["narHash"]
    return lines, narhash


def _merge(source: Path, data: bytes, found: Found, narhash: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    """Add what the parent knows to the JSON of the child: the exact line of each input, and `input_packages`.

    Returns the plain error lines too: an input that the list lacks, and an input that the syntax tree does not show.
    """
    compiled = json.loads(data)
    by_name = {i.name: i for i in found.inputs}
    listed = {i["name"] for i in compiled.get("inputs", [])}
    lines = [
        f"{source.name}:{i.line}:1: the input `{i.name}` is not in `Machine(inputs=[...])`. Add it to the list."
        for i in found.inputs
        if i.name not in listed
    ]
    # An `Input` that the syntax tree does not show (built in a helper, or with a value that is not a literal) has no
    # exact line and skips the pair check. Refuse it, at the line of `Machine(...)`.
    lines += [
        f"{item['source']}: the input `{item['name']}` is not written as "
        f'Input("{item["name"]}", "URL", ...) in {source.name}. '
        f"Write the Input call in {source.name} with literal values."
        for item in compiled.get("inputs", [])
        if item["name"] not in by_name
    ]
    for item in compiled.get("inputs", []):
        if item["name"] in by_name:
            item["source"] = by_name[item["name"]].source
    if narhash:
        compiled["input_packages"] = dict(sorted(narhash.items()))
    return compiled, lines


def compile_entry(source: Path, out: Path) -> int:
    """The parent: discover, pair check, two runs, compare, write."""
    name = source.name
    if not source.is_file():
        print(f"{name}:1:1: cannot read the file: No such file or directory.", file=sys.stderr)
        return 1
    found, lines = _discover(source)
    narhash: dict[str, str] = {}
    if found is not None:
        pair_lines, narhash = _pair_check(found, source.resolve().parent)
        lines += pair_lines
    if found is None or lines:
        print("\n".join(lines), file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="basix-compile-") as folder:
        try:
            runs = [_run_child(source, out, Path(folder), number) for number in (1, 2)]
        except subprocess.TimeoutExpired:
            print(
                f"{name}:1:1: the file ran for more than {CHILD_TIMEOUT_S} seconds. Remove the code that waits.",
                file=sys.stderr,
            )
            return 1
    if runs[0] != runs[1]:
        print(
            f"{name}:1:1: the file gives another result each time it runs. "
            "Remove the code that reads the clock, a random number, or the environment.",
            file=sys.stderr,
        )
        return 1
    code, text, data = runs[0]
    if text:
        print(text, end="", file=sys.stderr)
    if code != 0:
        return code
    compiled, lines = _merge(source, data, found, narhash)
    if lines:
        print("\n".join(lines), file=sys.stderr)
        return 1
    out.write_text(json.dumps(compiled, indent=1, sort_keys=True) + "\n")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) == 5 and argv[1] == "--run":
        return run_entry(Path(argv[2]), Path(argv[3]), Path(argv[4]))
    if len(argv) != 3:
        print("usage: python -m basix_config.compile system.py system.json", file=sys.stderr)
        return 2
    return compile_entry(Path(argv[1]), Path(argv[2]))


if __name__ == "__main__":
    sys.exit(main(sys.argv))
