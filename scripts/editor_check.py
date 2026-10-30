#!/usr/bin/env python3
"""The check `opt:basix-editor`: editor help in a fresh Basix workspace (BSX-371).

Run it from the repository root, through the devenv: `devenv shell -- uv run --no-sync python scripts/editor_check.py`.
The check needs no VM and no `/dev/kvm`. It never counts for green.

It makes a workspace the way `basix install` makes it (`basix.install._write_system`: the managed stub, the lock, and
`system.py`). The Basix input of the stub is a copy of this working tree. Then it starts the launcher of the workspace
lock, `nix run --inputs-from . basix#editor` (`nix/editor.nix`), speaks the language server protocol to it over
standard input and output, and checks four things on `system.py`:

- completion in the call `Machine(` offers the keywords (the fields of `Machine`),
- completion of a name offers the capability `RemoteLogin` (`from basix_config import *` resolves),
- hover on the keyword `state_version` shows the field help,
- a wrong value (`name=1`) gives a type error with the line.

A last check shows that the launcher follows the lock: a second workspace locks a changed copy of the tree, and its
launcher is another launcher. The check does not need the network: the build finds every input in the store and in
the fetcher cache. VM files and scratch go under `$BASIX_WORK` (default `~/.cache/basix/v2/editor`). Raw logs go
under `$BASIX_LOGS`.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))

import vmlib  # noqa: E402

from basix.install import _write_system  # noqa: E402
from basix_config.stub import make_stub  # noqa: E402

RESULTS: list[tuple[bool, str]] = []
IGNORED = (".git", ".jj", ".basix", ".devenv", ".direnv", ".gitman", ".scratch", ".loci", ".ruff_cache", "__pycache__")
REPLY_S = 120.0  # seconds that the server gets for one reply (the first one loads the libraries)


def report(ok: bool, text: str) -> bool:
    RESULTS.append((ok, text))
    print(("PASS " if ok else "FAIL ") + text, flush=True)
    return ok


def run(argv: list[str], cwd: Path, log: Path, timeout: float = 900) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False, timeout=timeout)
    with log.open("a") as handle:
        handle.write(f"$ {' '.join(argv)}\n(cwd {cwd}) exit {done.returncode}\n{done.stdout}\n{done.stderr}\n")
    return done


class Server:
    """A language server on a pipe. A thread reads the frames, so a wait has a time limit."""

    def __init__(self, argv: list[str], cwd: Path, stderr_log: Path) -> None:
        self.proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_log.open("wb"),
        )
        self.frames: queue.Queue[dict | None] = queue.Queue()
        self.seq = 0
        self.diagnostics: dict[str, list[dict]] = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        out = self.proc.stdout
        assert out is not None
        while True:
            length = None
            while True:
                line = out.readline()
                if not line:
                    self.frames.put(None)
                    return
                if not line.strip():
                    break
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            if length is not None:
                self.frames.put(json.loads(out.read(length)))

    def _write(self, message: dict) -> None:
        assert self.proc.stdin is not None
        raw = json.dumps(message, separators=(",", ":")).encode()
        self.proc.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
        self.proc.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def next_frame(self, timeout: float) -> dict:
        try:
            frame = self.frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("the language server sent nothing in time") from None
        if frame is None:
            raise RuntimeError("the language server closed its output")
        return frame

    def handle(self, frame: dict) -> None:
        """Answer a request of the server with an empty result; keep the diagnostics it pushes."""
        if "method" in frame and "id" in frame:
            self._write({"jsonrpc": "2.0", "id": frame["id"], "result": None})
        elif frame.get("method") == "textDocument/publishDiagnostics":
            params = frame["params"]
            self.diagnostics[params["uri"]] = params.get("diagnostics", [])

    def request(self, method: str, params: dict, timeout: float = REPLY_S) -> object:
        self.seq += 1
        want = self.seq
        self._write({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
        end = time.monotonic() + timeout
        while True:
            frame = self.next_frame(max(0.1, end - time.monotonic()))
            if frame.get("id") == want and "method" not in frame:
                if "error" in frame:
                    raise RuntimeError(f"{method} failed: {frame['error']}")
                return frame.get("result")
            self.handle(frame)

    def wait_diagnostics(self, uri: str, since: int, timeout: float = REPLY_S) -> list[dict]:
        """Wait until the server has pushed diagnostics for `uri` after the call number `since`."""
        self.diagnostics.pop(uri, None)
        end = time.monotonic() + timeout
        while uri not in self.diagnostics:
            self.handle(self.next_frame(max(0.1, end - time.monotonic())))
        return self.diagnostics[uri]

    def close(self) -> None:
        try:
            self.request("shutdown", {}, 20)
            self.notify("exit", {})
            self.proc.wait(timeout=20)
        except Exception:  # noqa: BLE001 - the server ends in any case
            self.proc.kill()


def make_workspace(work: Path, name: str, source: Path, logs: Path) -> Path:
    """A workspace as `basix install` writes it. Its Basix input is the folder `source`."""
    ws = work / name
    ws.mkdir(parents=True)
    lock_dir = work / f"{name}-lock"
    lock_dir.mkdir()
    (lock_dir / "flake.nix").write_text(make_stub(basix_url=f"path:{source}").text)
    log = logs / f"{name}.log"
    if run(["nix", "flake", "lock", "--offline", "--no-warn-dirty"], lock_dir, log).returncode:
        raise SystemExit(f"flake lock failed; see {log}")
    _write_system(ws, "basix", "learner", "/dev/disk/by-id/nvme-EXAMPLE", "26.11", lock_dir / "flake.lock", source)
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "learner"],
        ["git", "config", "user.email", "learner@basix"],
        ["git", "add", "-A"],
        ["git", "commit", "-q", "-m", "Install Basix"],
    ):
        run(command, ws, log)
    return ws


def alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def field_help(source: Path, field: str) -> str:
    """The first words of the help of a field of `Machine`: the `Args:` line in `machine.py` (the text that ty shows)."""
    text = (source / "src/basix_config/machine.py").read_text()
    match = re.search(rf"^\s+{field}: (.+)$", text[text.rindex("Args:") :], re.MULTILINE)
    if match is None:
        raise SystemExit(f"machine.py has no help line for {field}")
    return alnum(match.group(1))[:40]


def hover_text(result: object) -> str:
    if not isinstance(result, dict):
        return ""
    contents = result.get("contents")
    return contents.get("value", "") if isinstance(contents, dict) else str(contents or "")


def labels(result: object) -> set[str]:
    items = result.get("items", []) if isinstance(result, dict) else result or []
    return {item["label"] for item in items}


def probe(ws: Path, source: Path, logs: Path, tag: str) -> bool:
    """Start the launcher of the workspace lock and ask for completion, hover, and diagnostics."""
    log = logs / f"{tag}-build.log"
    started = time.monotonic()
    built = run(
        ["nix", "build", "--inputs-from", ".", "--out-link", str(ws.parent / f"{tag}-editor"), "--no-warn-dirty", "basix#editor"],
        ws,
        log,
    )
    report(built.returncode == 0, f"{tag}: `nix build --inputs-from . basix#editor` builds the launcher ({time.monotonic() - started:.0f} s)")
    if built.returncode:
        return False
    launcher = ws.parent / f"{tag}-editor" / "bin" / "basix-ty"
    text = (ws / "system.py").read_text()
    uri = (ws / "system.py").as_uri()
    root = ws.as_uri()
    server = Server([str(launcher)], ws, logs / f"{tag}-ty.stderr")
    started = time.monotonic()
    try:
        reply = server.request(
            "initialize",
            {
                "processId": None,
                "rootUri": root,
                "capabilities": {"textDocument": {"completion": {"completionItem": {"documentationFormat": ["markdown"]}}}},
                "workspaceFolders": [{"uri": root, "name": "basix"}],
            },
        )
        report(isinstance(reply, dict) and "capabilities" in reply, f"{tag}: the server answers `initialize`")
        server.notify("initialized", {})

        # Completion in `Machine(`: a blank line with the keyword prefix `tr` inside the call.
        lines = text.splitlines()
        call = next(i for i, line in enumerate(lines) if "Machine(" in line)
        typed = [*lines[: call + 1], "    tr", *lines[call + 1 :]]
        document = "\n".join(typed) + "\n"
        server.notify("textDocument/didOpen", {"textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": document}})
        server.wait_diagnostics(uri, 1)
        done = server.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": {"line": call + 1, "character": 6}, "context": {"triggerKind": 1}})
        found = labels(done)
        report("trusted_interfaces=" in found, f"{tag}: completion in `Machine(` offers `trusted_interfaces=` (got {len(found)} items)")
        first = time.monotonic() - started
        report(first < 60, f"{tag}: launch to first completion took {first:.1f} s")

        # Completion of a name: the capability, through `from basix_config import *`.
        named = ["from basix_config import *", "x = Remote", *lines[1:]]
        server.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": 2}, "contentChanges": [{"text": "\n".join(named) + "\n"}]})
        done = server.request("textDocument/completion", {"textDocument": {"uri": uri}, "position": {"line": 1, "character": len("x = Remote")}, "context": {"triggerKind": 1}})
        found = labels(done)
        report("RemoteLogin" in found, f"{tag}: completion of `Remote` offers `RemoteLogin` (got {len(found)} items)")

        # Hover on the keyword `state_version` in the call.
        server.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": 3}, "contentChanges": [{"text": text}]})
        row = next(i for i, line in enumerate(lines) if line.strip().startswith("state_version="))
        done = server.request("textDocument/hover", {"textDocument": {"uri": uri}, "position": {"line": row, "character": lines[row].index("state_version") + 3}})
        shown = hover_text(done)
        want = field_help(source, "state_version")
        report(want in alnum(shown), f"{tag}: hover on `state_version` shows the field help (hover text {shown[:160]!r})")

        # A type error: `name=1`.
        wrong = text.replace('name="basix"', "name=1", 1)
        row = next(i for i, line in enumerate(wrong.splitlines()) if "name=1" in line)
        server.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": 4}, "contentChanges": [{"text": wrong}]})
        problems = server.wait_diagnostics(uri, 4)
        lines_hit = [d for d in problems if d["range"]["start"]["line"] == row]
        shown_problem = lines_hit[0]["message"].splitlines()[0] if lines_hit else ""
        report(bool(lines_hit) and "Literal[1]" in " ".join(d["message"] for d in lines_hit), f"{tag}: `name=1` gives a type error on its line (got {shown_problem[:160]!r})")

        # The good text has no error.
        server.notify("textDocument/didChange", {"textDocument": {"uri": uri, "version": 5}, "contentChanges": [{"text": text}]})
        clean = server.wait_diagnostics(uri, 5)
        errors = [d for d in clean if d.get("severity") == 1]
        report(not errors, f"{tag}: the install text of `system.py` has no error (got {[d['message'][:80] for d in errors]})")
    except (RuntimeError, TimeoutError, OSError) as error:
        report(False, f"{tag}: {type(error).__name__}: {str(error)[:300]}")
        return False
    finally:
        server.close()
    return all(ok for ok, _ in RESULTS)


def copy_tree(target: Path) -> Path:
    shutil.copytree(ROOT, target, ignore=shutil.ignore_patterns(*IGNORED))
    return target


def main() -> int:
    os.environ.setdefault("BASIX_WORK", str(vmlib.workdir.default_work("v2/editor")))
    logs = vmlib.Logs.create("editor_check").root
    work = vmlib.work_dir("editor")
    print(f"editor_check: files {work}; logs {logs}", flush=True)

    source = copy_tree(work / "basix-source")
    ws = make_workspace(work, "ws", source, logs)
    report((ws / "flake.lock").is_file() and (ws / "system.py").is_file(), "the workspace holds flake.lock and system.py as the install writes them")
    ok = probe(ws, source, logs, "workspace")

    # The launcher follows the lock: a second workspace locks a second copy of the tree, and its launcher runs that copy.
    second = copy_tree(work / "basix-source-two")
    marker = "The lock-aware probe"
    machine_file = second / "src/basix_config/machine.py"
    old = machine_file.read_text()
    if "The release of NixOS that first installed this machine" in old:
        machine_file.write_text(old.replace("The release of NixOS that first installed this machine", marker, 1))
    ws2 = make_workspace(work, "ws2", second, logs)
    built = run(["nix", "build", "--inputs-from", ".", "--out-link", str(work / "second-editor"), "--no-warn-dirty", "basix#editor"], ws2, logs / "second-build.log")
    launcher = (work / "second-editor" / "bin" / "basix-ty").read_text() if built.returncode == 0 else ""
    first_launcher = (work / "workspace-editor" / "bin" / "basix-ty").read_text() if (work / "workspace-editor").exists() else ""
    report(
        built.returncode == 0 and launcher != "" and launcher != first_launcher,
        "the launcher of a workspace with another lock is another launcher: it follows the lock",
    )

    failed = [text for ok_, text in RESULTS if not ok_]
    print(f"{len(RESULTS) - len(failed)} passed, {len(failed)} failed; logs {logs}")
    print("GREEN" if not failed and ok else "RED")
    if not failed:
        shutil.rmtree(work, ignore_errors=True)
    else:
        print(f"scratch kept: {work}")
    return 1 if failed or not ok else 0


if __name__ == "__main__":
    sys.exit(main())
