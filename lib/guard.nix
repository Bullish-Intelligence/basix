# SG38 (proposed): the staleness guard. It returns null when the JSON is fresh, or one plain line.
# The compile wrote `source_sha256` (the entry) and `reads` (every file and folder that the entry read; see
# `pynix_reads.py`). This function hashes the same things again. The first difference stops the evaluation.
{ lib }:
{ data       # the decoded JSON
, source     # the entry file in the workspace (a path)
, name       # "home.py" or "system.py"
, command    # "minix apply" or "basix apply"
}:
let
  dir = builtins.dirOf source;
  at = rel: if rel == "." then dir else dir + "/${rel}";
  stale = what: "${what} changed: run `${command}`";
  reads = data.reads or null;
  fileProblem = rel: sha:
    if !builtins.pathExists (at rel)
    then "${rel} is not in the flake. Run `git add ${rel}`, then `${command}`"
    else if builtins.hashFile "sha256" (at rel) != sha then stale rel
    else null;
  absentProblem = rel:
    if builtins.pathExists (at rel) then "${rel} appeared (the last compile did not find it): run `${command}`" else null;
  namesOf = rel: lib.filter (n: !(builtins.elem n reads.ignore)) (builtins.attrNames (builtins.readDir (at rel)));
  dirProblem = rel: names:
    if !builtins.pathExists (at rel) then "${rel}/ is not in the flake. Run `git add ${rel}`, then `${command}`"
    else if namesOf rel != names then "${rel}/ now holds other files: run `${command}`"
    else null;
  problems =
    lib.optional (!(data ? source_sha256 && data.source_sha256 == builtins.hashFile "sha256" source)) (stale name)
    ++ (if reads == null then [ (stale name) ] else
      lib.filter (p: p != null) (
        lib.mapAttrsToList fileProblem reads.files
        ++ map absentProblem reads.absent
        ++ lib.mapAttrsToList dirProblem reads.dirs));
in
if problems == [ ] then null else builtins.head problems
