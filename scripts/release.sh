#!/usr/bin/env bash
# release.sh: the release job of Basix. The names are proposed.
# Usage: scripts/release.sh [--skip-status] TAG        (TAG is vX.Y.Z or vX.Y.Z-rc.N; X.Y.Z must equal the version in pyproject.toml)
#
# Run it through the devenv (devenv shell -- scripts/release.sh TAG) from a clean checkout of the commit to tag. The
# devenv gives the Git identity, uv, and the pinned tools. The job never pushes. The release commit needs a GREEN
# `testee verify` before the owner pushes it.
#
# Steps:
#   0  refuse unless `testee status` of the commit at HEAD is GREEN (by commit id: jj does not know `HEAD`);
#      then run `uv sync --locked`, so the job has its own Python environment (Testee does not run for this script)
#   1  refuse a tag with a wrong form, a tag that exists, a dirty tree, and a tag whose X.Y.Z differs from the version
#      in pyproject.toml (010-D31). These checks cost nothing, so they run before step 0.
#   2  check the locks: `uv lock --check`, and `nix flake lock --no-update-lock-file` (it fails when flake.lock does not
#      agree with flake.nix; `nix flake metadata --no-update-lock-file` fails the same way, but it prints more)
#   3  check the pair (009-D28): the nixpkgs revision that `pynix_os` records in the locked `pynix` against the nixpkgs of
#      flake.lock; then run the `guards` step of `scripts/check_eval.py` (it evaluates the pair guard of `lib.machine`);
#      then compare the pins of this lock with the lock of the newest minix tag (010-D33, BSX-389). The compare only warns.
#   4  write provenance/inputs.json and the changelog text; commit provenance/ and CHANGELOG.md as one release commit
#   5  make the annotated tag TAG on the release commit; its message holds the changelog section
#   6  print the push commands; the job does not push
#
# --skip-status skips the Testee part of step 0. Use it only to test this script on a scratch clone, where no verdict exists.
#
# Environment:
#   BASIX_RELEASE_WORK    work directory (default: a new directory under $TMPDIR; the job deletes it at the end)
#   BASIX_RELEASE_BRANCH  the branch in the printed push line (default release, 010-D54)
#   BASIX_RELEASE_ALIAS   an alias branch, which the job moves to the same commit (default: none, 012-D5)
#   BASIX_RELEASE_PYNIX_URL, BASIX_RELEASE_MINIX_URL
#                         the public URLs that the layer compare fetches (default: the GitHub URLs of pynix and minix)
#   BASIX_RELEASE_LOG     directory for the raw logs (default ~/.local/state/basix/basix-release/TAG)
#   BASIX_RELEASE_PYNIX   test use only: a directory to read pynix_os from, in place of the locked pynix source
#
# Exit codes: 0 released; 2 wrong use or a failed precondition; 4 a step failed. A failed step after the release commit
# undoes the commit and the tag.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

skip_status=0
tag=
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-status) skip_status=1; shift ;;
    -*) echo "release: unknown option $1" >&2; exit 2 ;;
    *) [ -z "$tag" ] || { echo "release: one TAG only" >&2; exit 2; }; tag=$1; shift ;;
  esac
done
[ -n "$tag" ] || { grep -m1 '^# Usage:' "$0" >&2; exit 2; }
if [[ ! $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-rc\.[0-9]+)?$ ]]; then
  echo "release: the tag must look like vX.Y.Z or vX.Y.Z-rc.N, not $tag" >&2
  exit 2
fi
version=${tag#v}
branch=${BASIX_RELEASE_BRANCH:-release}
alias_branch=${BASIX_RELEASE_ALIAS-}
alias_line=
if [ -n "$alias_branch" ] && [ "$alias_branch" != "$branch" ]; then
  alias_line="  git push origin '$tag^{commit}:refs/heads/$alias_branch'"
fi

root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
log=${BASIX_RELEASE_LOG:-$HOME/.local/state/basix/basix-release/$tag}
mkdir -p "$log"
if [ -n "${BASIX_RELEASE_WORK:-}" ]; then
  work=$BASIX_RELEASE_WORK
  keep_work=1
else
  work=$(mktemp -d "${TMPDIR:-/tmp}/basix-release.XXXXXX")
  keep_work=0
fi
rm -rf "$work"
mkdir -p "$work"

say() { echo "[release $tag] $*" | tee -a "$log/run.txt"; }
die() { say "STOP: $*"; exit 4; }

orig=
committed=0
made_tags=()
cleanup() {
  rc=$?
  if [ "$rc" != 0 ]; then
    for t in "${made_tags[@]}"; do git tag -d "$t" >/dev/null 2>&1 || true; done
    if [ "$committed" = 1 ] && [ -n "$orig" ]; then
      git reset --hard -q "$orig" && say "undid the release commit (back to $orig)"
    fi
    [ "$rc" = 2 ] || [ "$rc" = 4 ] || { say "STOP: a command failed (exit $rc); see $log"; rc=4; }
  fi
  [ "$keep_work" = 1 ] || rm -rf "$work"
  exit "$rc"
}
trap cleanup EXIT

# --- 1. preconditions -------------------------------------------------------------------------------------------
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "release: not a Git work tree" >&2; exit 2; }
[ -z "$(git status --porcelain)" ] || { echo "release: the checkout is not clean; use a clean checkout of the commit to tag" >&2; exit 2; }
if ! { git var GIT_AUTHOR_IDENT && git var GIT_COMMITTER_IDENT; } >/dev/null 2>&1; then
  echo "release: no Git identity; run through the devenv (devenv shell -- scripts/release.sh $tag)" >&2
  exit 2
fi
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && { echo "release: the tag $tag exists; a release tag never moves" >&2; exit 2; }
# The first `version` line after `[project]` is the version of the package.
project_version=$(sed -n '/^\[project\]/,/^\[/{s/^version = "\(.*\)"[[:space:]]*$/\1/p}' pyproject.toml | head -1)
[ -n "$project_version" ] || { echo "release: no version in pyproject.toml" >&2; exit 2; }
if [ "${version%%-*}" != "$project_version" ]; then
  echo "release: the tag $tag names version ${version%%-*}, but pyproject.toml has version $project_version" >&2
  exit 2
fi
orig=$(git rev-parse HEAD)
say "start: HEAD $orig, work $work"

# --- 0. the verdict of Testee -----------------------------------------------------------------------------------
if [ "$skip_status" = 1 ]; then
  say "0 testee status skipped (--skip-status: test use only)"
else
  head=$(git rev-parse HEAD)
  testee status -r "$head" >"$log/testee-status.txt" 2>&1 || { cat "$log/testee-status.txt" >&2; die "testee status -r $head is not GREEN"; }
  say "0 testee status GREEN: $(tail -1 "$log/testee-status.txt")"
fi

# --- 0. the Python environment ----------------------------------------------------------------------------------
# A new devenv Python makes devenv create an empty venv. Testee runs `setup:sync` for itself, but not for this script.
say "0 uv sync --locked"
uv sync --locked >"$log/uv-sync.txt" 2>&1 || { tail -5 "$log/uv-sync.txt" >&2; die "uv sync --locked failed (see $log/uv-sync.txt)"; }
PY=$(uv run --no-sync python -c 'import sys; print(sys.executable)') || { echo "release: no uv environment; run uv sync" >&2; exit 2; }
export PY

# --- the previous release ---------------------------------------------------------------------------------------
# The highest release tag below TAG, in version order (a release candidate sorts below its release).
mapfile -t existing < <(git tag -l 'v*' || true)
prev=$("$PY" -I - "$tag" "${existing[@]}" <<'PYEOF'
import re
import sys

def key(t):
    m = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-rc\.(\d+))?", t)
    return (int(m[1]), int(m[2]), int(m[3]), 1 if m[4] is None else 0, int(m[4] or 0)) if m else None

tag, others = sys.argv[1], sys.argv[2:]
below = sorted((t for t in others if key(t) and key(t) < key(tag)), key=key)
print(below[-1] if below else "")
PYEOF
)
if [ -n "$prev" ]; then
  say "previous release: $prev"
else
  say "previous release: none (first release)"
fi

# --- 2. the locks -----------------------------------------------------------------------------------------------
say "2 uv lock --check"
uv lock --check >"$log/uv-lock.txt" 2>&1 || { tail -5 "$log/uv-lock.txt" >&2; die "uv.lock is stale (see $log/uv-lock.txt)"; }
say "2 nix flake lock --no-update-lock-file"
# This command exits non-zero when flake.lock needs a change. A stale flake.lock then stops the job.
nix flake lock --no-update-lock-file >"$log/flake-lock.txt" 2>&1 \
  || { tail -5 "$log/flake-lock.txt" >&2; die "flake.lock does not agree with flake.nix (see $log/flake-lock.txt)"; }
[ -z "$(git status --porcelain)" ] || die "a lock check wrote files into the checkout: $(git status --porcelain | head -3 | tr '\n' ' ')"

# --- 3. the pair ------------------------------------------------------------------------------------------------
say "3 pair check: pynix_os against flake.lock"
pynix_src=${BASIX_RELEASE_PYNIX:-}
if [ -z "$pynix_src" ]; then
  pynix_src=$(nix eval --raw --impure --expr \
    'let l = (builtins.fromJSON (builtins.readFile '"$root"'/flake.lock)).nodes.pynix.locked; in (builtins.fetchTree l).outPath' \
    2>>"$log/nix-eval.txt") || die "cannot read the locked pynix source (see $log/nix-eval.txt)"
fi
"$PY" -I - "$root/flake.lock" "$pynix_src/src/pynix_os/_generation.py" >"$log/pair.txt" 2>&1 <<'PYEOF' || { cat "$log/pair.txt" >&2; die "the pair check failed (see $log/pair.txt)"; }
import json
import re
import sys

lock = json.load(open(sys.argv[1]))["nodes"]
match = re.search(r'"nixpkgs":\s*"([0-9a-f]{40})"', open(sys.argv[2]).read())
recorded = match[1] if match else None
locked = lock["nixpkgs"]["locked"]["rev"]
print(f"pynix_os records: nixpkgs {recorded}")
print(f"flake.lock holds: nixpkgs {locked}")
if recorded != locked:
    sys.exit("pair: pynix_os records another nixpkgs than flake.lock holds")
PYEOF
say "3 $(sed -n 1p "$log/pair.txt")"
say "3 $(sed -n 2p "$log/pair.txt")"
say "3 scripts/check_eval.py, step guards (it evaluates the pair guard of lib.machine)"
BASIX_CHECK_STEPS=guards BASIX_CHECK_WORK=$work/check BASIX_CHECK_LOGS=$log/check "$PY" -I scripts/check_eval.py . >"$log/check-eval.txt" 2>&1 \
  || { grep -E '^(PASS|FAIL|SKIP)' "$log/check-eval.txt" >&2 || tail -5 "$log/check-eval.txt" >&2; die "scripts/check_eval.py (guards) failed (see $log/check-eval.txt)"; }
grep -E '^(PASS|FAIL|SKIP)' "$log/check-eval.txt" >"$work/check-lines.txt" || die "scripts/check_eval.py printed no check line (see $log/check-eval.txt)"
! grep -qv '^PASS' "$work/check-lines.txt" || die "a check line is not PASS (see $log/check-eval.txt)"
say "3 check_eval guards: $(wc -l <"$work/check-lines.txt") check lines, all PASS"
[ -z "$(git status --porcelain)" ] || die "the checks wrote files into the checkout: $(git status --porcelain | head -3 | tr '\n' ' ')"

# --- 3b. the layer compare (010-D33, BSX-389) -------------------------------------------------------------------
# It only warns: nothing here stops the release. The script `compare_layers.py` comes from `pynix`. The job fetches
# `pynix` and minix through their public URLs (the Git rules of the user map them to a local cache).
compare_layers() {
  local pynix_url=${BASIX_RELEASE_PYNIX_URL:-https://github.com/Bullish-Intelligence/pynix}
  local minix_url=${BASIX_RELEASE_MINIX_URL:-https://github.com/Bullish-Intelligence/minix}
  local cdir=$work/compare pin script mtag t
  mkdir -p "$cdir"
  git clone -q --bare "$pynix_url" "$cdir/pynix" >"$log/compare-fetch.txt" 2>&1 || { say "3 warning: no layer compare: cannot fetch pynix"; return 0; }
  git clone -q --bare "$minix_url" "$cdir/minix" >>"$log/compare-fetch.txt" 2>&1 || { say "3 warning: no layer compare: cannot fetch minix"; return 0; }
  mtag=$(git -C "$cdir/minix" -c versionsort.suffix=-rc tag -l 'v*' --sort=-v:refname | head -1)
  [ -n "$mtag" ] || { say "3 warning: no layer compare: minix has no release tag"; return 0; }
  git -C "$cdir/minix" show "$mtag:flake.lock" >"$cdir/minix-flake.lock" 2>>"$log/compare-fetch.txt" \
    || { say "3 warning: no layer compare: $mtag of minix has no flake.lock"; return 0; }
  # The pinned pynix tag first; else the newest pynix tag that holds the script.
  pin=$("$PY" -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["nodes"]["pynix"]["original"].get("ref","").removeprefix("refs/tags/"))' "$root/flake.lock" 2>/dev/null || true)
  script=
  for t in $pin $(git -C "$cdir/pynix" -c versionsort.suffix=-rc tag -l 'pynix-v*' --sort=-v:refname); do
    if git -C "$cdir/pynix" show "$t:scripts/compare_layers.py" >"$cdir/compare_layers.py" 2>/dev/null; then script=$t; break; fi
  done
  [ -n "$script" ] || { say "3 warning: no layer compare: no pynix tag holds scripts/compare_layers.py"; return 0; }
  say "3 layer compare: Basix flake.lock against minix $mtag (script from pynix tag $script)"
  "$PY" -I "$cdir/compare_layers.py" "$root/flake.lock" "$cdir/minix-flake.lock" --names basix,minix >"$log/compare.txt" 2>&1 \
    || say "3 warning: the layer compare failed (see $log/compare.txt)"
  while IFS= read -r line; do say "3 compare: $line"; done <"$log/compare.txt"
  return 0
}
compare_layers || say "3 warning: the layer compare stopped (see $log)"

# --- 4. provenance, changelog, release commit -------------------------------------------------------------------
say "4 provenance and changelog"
mkdir -p "$work/provenance"
git log --format=%s ${prev:+"$prev..HEAD"} >"$work/subjects.txt"
"$PY" -I - "$root" "$tag" "$version" "$orig" "$prev" "$work" "$pynix_src/src/pynix_os/_generation.py" <<'PYEOF'
import datetime
import json
import re
import sys
from pathlib import Path

root, tag, version, commit, prev, work, record = sys.argv[1:8]
work = Path(work)
nodes = json.loads((Path(root) / "flake.lock").read_text())["nodes"]


def pick(name, keys):
    node = nodes[name]["locked"]
    return {k: node[k] for k in keys}


pynix = pick("pynix", ("ref", "rev", "narHash"))
if not pynix["ref"].startswith("refs/tags/"):
    sys.exit("the pynix input must name a release tag, not " + pynix["ref"])
pynix = {"tag": pynix.pop("ref").removeprefix("refs/tags/"), **pynix}
nixpkgs = pick("nixpkgs", ("rev", "narHash", "lastModified"))
# The other inputs of the flake: each with its revision and hash.
others = {
    name: pick(name, ("rev", "narHash"))
    for name in sorted(nodes["root"]["inputs"])
    if name not in ("pynix", "nixpkgs")
}
match = re.search(r'"nixpkgs":\s*"([0-9a-f]{40})"', Path(record).read_text())
checks = (work / "check-lines.txt").read_text().splitlines()
out = {
    "tag": tag,
    "version": version,
    "commit": commit,
    "previous": prev or None,
    "pynix": pynix,
    "nixpkgs": nixpkgs,
    **others,
    "pynix_os": {"nixpkgs": match[1] if match else None},
    "checks": checks,
}
(work / "provenance" / "inputs.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")

date = datetime.datetime.fromtimestamp(nixpkgs["lastModified"], datetime.UTC).strftime("%Y-%m-%d")
lines = [f"Basix {version} (pynix {pynix['tag']}; nixpkgs {nixpkgs['rev'][:7]}, {date})"]
subjects = [s for s in (work / "subjects.txt").read_text().splitlines() if s.strip()]
if prev:
    lines.append(f"Previous: {prev}. Commits since then: {len(subjects)}.")
    lines += [f"- {s}" for s in subjects]
else:
    lines.append("Initial release.")
(work / "changelog.txt").write_text("\n".join(lines) + "\n")
PYEOF
say "4 changelog: $(wc -l <"$work/changelog.txt") lines"

rm -rf provenance
cp -r "$work/provenance" provenance
{
  echo "# Changelog"
  echo
  echo "## $tag"
  echo
  echo '```text'
  cat "$work/changelog.txt"
  echo '```'
  if [ -f CHANGELOG.md ]; then
    echo
    sed '1{/^# Changelog$/d}' CHANGELOG.md | sed '1{/^$/d}'
  fi
} >"$work/CHANGELOG.md"
cp "$work/CHANGELOG.md" CHANGELOG.md
git add provenance CHANGELOG.md
git commit -q -m "Release $tag: provenance and changelog" -m "$(sed -n 1p "$work/changelog.txt")"
committed=1
release_commit=$(git rev-parse HEAD)
[ -z "$(git status --porcelain)" ] || die "the release commit left files behind"
say "4 release commit $release_commit (tree $(git rev-parse 'HEAD^{tree}'))"

# --- 5. the release tag -----------------------------------------------------------------------------------------
{
  echo "Basix $version"
  echo
  cat "$work/changelog.txt"
  echo
  sed 's/^/check: /' "$work/check-lines.txt"
} >"$work/tag-message.txt"
git tag -a "$tag" -F "$work/tag-message.txt" "$release_commit"
made_tags+=("$tag")
say "5 tag $tag -> $release_commit"
committed=0
made_tags=()

# --- 6. the next command ----------------------------------------------------------------------------------------
say "released $tag (release commit $release_commit)"
cat <<EOF

Released $tag. Nothing was pushed. The release commit needs a GREEN \`testee verify\` before you push it.
Push the tag, then move the branch $branch to the same commit (a fast-forward on origin):

  git push origin $tag
  git push origin '$tag^{commit}:refs/heads/$branch'
$alias_line

Keep the release commit on the branch (fast-forward the trunk to $release_commit)
so that the next release finds provenance/ and CHANGELOG.md.
EOF
