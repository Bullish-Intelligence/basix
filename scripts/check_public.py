"""Check that no tracked file names an owner fact (BSX-415, 012-D12).

The check reads the files that Git tracks (`git ls-files`), not a folder walk. It fails on these shapes:

- an e-mail address (a reserved example domain, `git@HOST`, and the GitHub no-reply address pass);
- a home folder, `/home/NAME`;
- `~/vendor`;
- `/mnt/`;
- a `/dev/disk/by-id/` path that carries a serial (a name part of 8 or more characters with a letter and a digit, or
  5 digits in a row; `/dev/disk/by-id/nvme-EXAMPLE` passes);
- a private IP address (10.x, 172.16 to 172.31, 192.168.x, and 100.64 to 100.127);
- `.ts.net`;
- `git://`;
- a link into the records (`.loci` or `.scratch`).

Two files hold the data. Both are optional.

- The allow list `scripts/public_allow.toml` of the repository holds the reviewed exceptions. Each entry has `path`
  (a glob), `shape`, `text` (a part of the line), and `reason`. An entry without a reason is an error.
- The private pattern file names the owner words: one regular expression per line. The variable
  `PUBLIC_LEAK_PATTERNS` holds its path. The owner layer holds the file. A tracked file never holds an owner word.
  The word rules skip the generated trees of `pynix`. The shape rules do not.

`--require-notice` adds the rule of BSX-417: the file `NOTICE` exists and names nixpkgs, Home Manager, and flake-parts.

The three repositories (pynix, minix, Basix) each carry the same copy of this file. No copy imports another.

Usage:
    python3 -I scripts/check_public.py [ROOT] [--require-notice]
    python3 -I scripts/check_public.py --selftest
Exit code: 0 pass, 1 findings, 2 the check could not run.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

#: The shape rules. Each pattern is a regular expression on one line of text.
SHAPES: dict[str, re.Pattern[str]] = {
    "e-mail address": re.compile(r"(?<![\w.+-])[\w.+-]+@(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}\b"),
    "home folder": re.compile(r"/home/[A-Za-z_][\w.-]*"),
    "~/vendor": re.compile(r"~/vendor\b"),
    "/mnt/": re.compile(r"/mnt/"),
    "/dev/disk/by-id/": re.compile(r"/dev/disk/by-id/[\w.:-]+"),
    "private IP address": re.compile(
        r"(?<![\w.])(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})(?![\w.]*\w)"
    ),
    ".ts.net": re.compile(r"\.ts\.net\b"),
    "git://": re.compile(r"\bgit://"),
    "link into the records": re.compile(r"\]\([^)\s]*\.(?:loci|scratch)(?:/|\))"),
}

#: An e-mail address with one of these domains is not an owner fact (reserved domains, RFC 2606 and RFC 6761).
EMAIL_OK = re.compile(
    r"@(?:[\w.-]+\.)?(?:example\.(?:com|org|net)|example|invalid|test|localhost)$"
    r"|@users\.noreply\.github\.com$"
    r"|^git@(?:github\.com|gitlab\.com)$",
    re.IGNORECASE,
)

#: The generated trees of pynix. The word rules skip them. The shape rules do not.
GENERATED = ("src/pynix_os/", "src/pynix_hm/", "src/pynix_pkgs/")

#: This file names every shape in its own text. It is the only file that the rules skip.
SELF = "scripts/check_public.py"

#: The allow list quotes the shapes that it allows. The word rules read it. The shape rules skip it.
ALLOW = "scripts/public_allow.toml"

#: The three sources that `NOTICE` names (BSX-417).
NOTICE_SOURCES = ("nixpkgs", "home manager", "flake-parts")


def carries_serial(path: str) -> bool:
    """A by-id name with a serial: a part of 8 or more characters with a letter and a digit, or 5 digits in a row."""
    name = path.rsplit("/", 1)[-1]
    if re.search(r"\d{5,}", name):
        return True
    return any(
        len(part) >= 8 and re.search(r"[A-Za-z]", part) and re.search(r"\d", part) for part in re.split(r"[_:.-]", name)
    )


class Finding:
    def __init__(self, path: str, line: int, shape: str, text: str) -> None:
        self.path, self.line, self.shape, self.text = path, line, shape, text

    def show(self) -> str:
        return f"{self.path}:{self.line}: {self.shape}: {self.text.strip()[:140]}"


def tracked_files(root: Path) -> list[str]:
    """The tracked files, and the new files that Git does not ignore (a working copy not yet committed)."""
    result = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("git ls-files failed: " + result.stderr.decode(errors="replace").strip())
    names = sorted({n for n in result.stdout.decode().split("\0") if n})
    return [n for n in names if (root / n).is_file() and not (root / n).is_symlink()]


def load_allow(root: Path) -> tuple[list[dict[str, str]], list[str]]:
    path = root / "scripts" / "public_allow.toml"
    if not path.is_file():
        return [], []
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("allow", [])
    errors = []
    for number, entry in enumerate(entries, start=1):
        for key in ("path", "shape", "text", "reason"):
            if not str(entry.get(key, "")).strip():
                errors.append(f"scripts/public_allow.toml: entry {number} has no `{key}`")
    return entries, errors


def load_words(environ: dict[str, str]) -> list[re.Pattern[str]]:
    name = environ.get("PUBLIC_LEAK_PATTERNS", "")
    if not name:
        return []
    lines = Path(name).read_text(encoding="utf-8").splitlines()
    return [re.compile(line.strip(), re.IGNORECASE) for line in lines if line.strip() and not line.lstrip().startswith("#")]


def allowed(entries: list[dict[str, str]], path: str, shape: str, line: str) -> bool:
    return any(
        fnmatch.fnmatchcase(path, e["path"]) and e["shape"] == shape and e["text"] in line for e in entries
    )


def scan_text(path: str, text: str, entries: list[dict[str, str]], words: list[re.Pattern[str]]) -> list[Finding]:
    findings: list[Finding] = []
    generated = path.startswith(GENERATED)
    for number, line in enumerate(text.splitlines(), start=1):
        for shape, pattern in () if path == ALLOW else SHAPES.items():
            for match in pattern.finditer(line):
                if shape == "e-mail address" and EMAIL_OK.search(match.group(0)):
                    continue
                if shape == "/dev/disk/by-id/" and not carries_serial(match.group(0)):
                    continue
                if not allowed(entries, path, shape, line):
                    findings.append(Finding(path, number, shape, line))
                break
        if not generated:
            for word in words:
                if word.search(line) and not allowed(entries, path, "owner word", line):
                    findings.append(Finding(path, number, "owner word", "(matches a private pattern)"))
                    break
    return findings


def notice_errors(root: Path) -> list[str]:
    path = root / "NOTICE"
    if not path.is_file():
        return ["NOTICE: missing (BSX-417)"]
    text = re.sub(r"\s+", " ", path.read_text(encoding="utf-8").lower())
    return [f"NOTICE: does not name {name} (BSX-417)" for name in NOTICE_SOURCES if name not in text]


def check(root: Path, environ: dict[str, str], require_notice: bool = False) -> tuple[int, list[str]]:
    entries, errors = load_allow(root)
    words = load_words(environ)
    files = tracked_files(root)
    skipped = 0
    for name in files:
        if name == SELF:
            continue
        try:
            text = (root / name).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            skipped += 1
            continue
        errors.extend(f.show() for f in scan_text(name, text, entries, words))
    if require_notice:
        errors.extend(notice_errors(root))
    return len(files) - skipped - (SELF in files), errors


# --- self test ------------------------------------------------------------------------------------------------------


def selftest() -> int:
    # The samples are built from parts, so this file holds no shape itself.
    home, mnt, tilde = "/ho" + "me/", "/m" + "nt/", "~" + "/ven" + "dor"
    at, dot = "@", "."
    bad = {
        "e-mail address": "write to jane" + at + "corp" + dot + "io for help",
        "home folder": "cd " + home + "jane/work",
        "~/vendor": "clone " + tilde + "/repo",
        "/mnt/": "path " + mnt + "disk/x",
        "/dev/disk/by-id/": "disk /dev/disk/" + "by-id/nvme-Disk_4TB_S7KGNU0X123456P",
        "private IP address": "host 192" + ".168.1.20 and 10" + ".1.2.3 and 100" + ".101.2.3",
        ".ts.net": "name box.tail1234" + ".ts" + ".net",
        "git://": "url gi" + "t://box/repo",
        "link into the records": "see [x](.lo" + "ci/CURRENT.md)",
    }
    good = [
        "plain text with .loci/CURRENT.md and no link",
        "mail jane" + at + "example" + dot + "org is a reserved domain",
        "remote git" + at + "github" + dot + "com:Org/repo.git",
        "author 1+Org" + at + "users.noreply.github" + dot + "com",
        "the address 8.8.8.8 and 172.32.0.1 and 100.128.0.1 are public",
        "disk /dev/disk/by-id/nvme-EXAMPLE and /dev/disk/by-id/virtio-vd-target and /dev/disk/by-id/disk0",
        "a decorator " + at + "pytest.fixture and an input name" + at + "v1",
    ]
    failures = 0

    def expect(label: str, ok: bool) -> None:
        nonlocal failures
        print(("PASS" if ok else "FAIL") + " selftest " + label)
        failures += 0 if ok else 1

    for shape, line in bad.items():
        shapes = {f.shape for f in scan_text("a.md", line, [], [])}
        expect(f"bad sample fails: {shape}", shape in shapes)
    for line in good:
        expect("good sample passes: " + line[:40], not scan_text("a.md", line, [], []))
    # allow list
    entry = [{"path": "scripts/*.py", "shape": "/mnt/", "text": "disk/x", "reason": "r"}]
    expect("allow list passes a reviewed line", not scan_text("scripts/a.py", bad["/mnt/"], entry, []))
    expect("allow list is per path", bool(scan_text("docs/a.md", bad["/mnt/"], entry, [])))
    # private patterns
    words = [re.compile("zebrafish", re.IGNORECASE)]
    expect("a private pattern fails", bool(scan_text("a.md", "my ZebraFish host", [], words)))
    expect("a private pattern skips generated trees", not scan_text(GENERATED[0] + "a.py", "zebrafish", [], words))
    expect("a shape rule covers generated trees", bool(scan_text(GENERATED[0] + "a.py", bad["git://"], [], words)))
    # a repository: git ls-files, the allow list, NOTICE
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        git = ["git", "-C", folder]
        subprocess.run([*git, "init", "-q"], check=True)
        (root / "ok.md").write_text("fine\n")
        (root / "scripts").mkdir()
        (root / "ignored").write_text("x\n")
        (root / ".gitignore").write_text("ignored\n")
        (root / "ignored").write_text(bad["git://"] + "\n")
        count, errors = check(root, {})
        expect("a clean repository passes", not errors and count == 2)
        (root / "bad.md").write_text(bad["git://"] + "\n")
        count, errors = check(root, {})
        expect("a bad file fails with its line", len(errors) == 1 and errors[0].startswith("bad.md:1: git://"))
        (root / "scripts" / "public_allow.toml").write_text('[[allow]]\npath = "bad.md"\nshape = "git://"\ntext = "x"\n')
        expect("an allow entry without a reason is an error", any("no `reason`" in e for e in check(root, {})[1]))
        (root / "scripts" / "public_allow.toml").write_text(
            '[[allow]]\npath = "bad.md"\nshape = "git://"\ntext = "box"\nreason = "test"\n'
        )
        expect("an allow entry with a reason passes the line", not check(root, {})[1])
        words_file = Path(folder + ".words")
        words_file.write_text("# comment\nunicorn\n")
        (root / "w.md").write_text("a unicorn\n")
        errors = check(root, {"PUBLIC_LEAK_PATTERNS": str(words_file)})[1]
        words_file.unlink()
        expect("the private pattern file works", len(errors) == 1 and errors[0].startswith("w.md:1: owner word"))
        expect("NOTICE missing fails", check(root, {}, require_notice=True)[1][-1].startswith("NOTICE: missing"))
        (root / "NOTICE").write_text("nixpkgs and Home\nManager\n")
        expect("NOTICE needs all three sources", any("flake-parts" in e for e in check(root, {}, True)[1]))
        (root / "NOTICE").write_text("nixpkgs, Home Manager, flake-parts\n")
        expect("NOTICE with the three sources passes", not [e for e in check(root, {}, True)[1] if "NOTICE" in e])
    print(f"selftest: {failures} failures")
    return 1 if failures else 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    args = [a for a in argv[1:] if not a.startswith("--")]
    root = Path(args[0] if args else ".").resolve()
    try:
        count, errors = check(root, dict(os.environ), "--require-notice" in argv)
    except (RuntimeError, OSError, re.error, tomllib.TOMLDecodeError) as error:
        print(f"check_public: cannot run: {error}", file=sys.stderr)
        return 2
    for error in errors:
        print(error, file=sys.stderr)
    print(f"check_public: {count} files, {len(errors)} findings")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
