"""Check the Markdown files of the repository.

Fails when:
- a relative Markdown link does not resolve to a file or directory;
- a relative Markdown link points into the records (`.loci` or `.scratch`, BSX-418);
- a `BSX-` ID appears twice as a table row;
- a table row has an ID with a prefix other than `BSX-`.

A link that leaves the repository (for example into `../vendomat`) is not checked,
because the target may not exist in a clean checkout.

The folder `.loci` is skipped, so the check passes on a tree without records. The owner checks the ledger of the
records with `python3 -I scripts/check_docs.py .loci/`.

Usage: python3 -I scripts/check_docs.py [ROOT]
"""

import re
import sys
from pathlib import Path

SKIP_DIRS = {".git", ".jj", ".devenv", ".direnv", ".gitman", ".loci", ".testee", ".venv", "node_modules"}
#: The records are not part of the tracked tree. A link into them breaks in a clone (BSX-418).
RECORD_DIRS = {".loci", ".scratch"}
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
ROW_ID = re.compile(r"^\|\s*`?([A-Z]+)-(\d+)`?\s*\|")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def markdown_files(root: Path):
    for path in sorted(root.rglob("*.md")):
        if not SKIP_DIRS.intersection(path.relative_to(root).parts):
            yield path


def check_file(path: Path, root: Path, seen: dict[str, str], errors: list[str]) -> None:
    in_fence = False
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        where = f"{path.relative_to(root)}:{number}"
        for target in LINK.findall(line):
            if SCHEME.match(target) or target.startswith("#"):
                continue
            resolved = (path.parent / target.split("#", 1)[0]).resolve()
            if root != resolved and root not in resolved.parents:
                continue
            if root in resolved.parents and resolved.relative_to(root).parts[0] in RECORD_DIRS:
                errors.append(f"{where}: link into the records: {target}")
                continue
            if not resolved.exists():
                errors.append(f"{where}: link does not resolve: {target}")
        row = ROW_ID.match(line)
        if row:
            prefix, digits = row.groups()
            requirement = f"{prefix}-{digits}"
            if prefix != "BSX":
                errors.append(f"{where}: row uses prefix {prefix}-, not BSX-: {requirement}")
            elif requirement in seen:
                errors.append(f"{where}: {requirement} is already a row at {seen[requirement]}")
            else:
                seen[requirement] = where


#: BSX-324: a guide for the user is one screen and shows no Nix code.
GUIDE_LINES = 40


def check_guides(root: Path, errors: list[str]) -> int:
    guides = sorted((root / "capabilities").glob("*/lesson.md"))
    if (root / "template" / "README.md").is_file():
        guides.append(root / "template" / "README.md")
    for path in guides:
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(root)
        if len(text.splitlines()) > GUIDE_LINES:
            errors.append(f"{name}: a guide is {GUIDE_LINES} lines or fewer (BSX-324)")
        if "```nix" in text:
            errors.append(f"{name}: a guide shows no Nix code (BSX-324)")
    return len(guides)


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    seen: dict[str, str] = {}
    errors: list[str] = []
    files = list(markdown_files(root))
    for path in files:
        check_file(path, root, seen, errors)
    guides = check_guides(root, errors)
    for error in errors:
        print(error, file=sys.stderr)
    print(f"check_docs: {len(files)} files, {guides} guides, {len(seen)} BSX rows, {len(errors)} errors")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
