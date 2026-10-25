# The source trees of the Basix outputs. This file is the one place that lists which files an output copies.
#
#   lib   the nixpkgs library
#   root  the Basix source folder (the flake `self`, or a checkout)
#
# A tree holds only the files that its output reads. A tool of the developer can leave other files in the checkout:
# Python bytecode (`__pycache__`, `*.pyc`), a tool cache, an editor file, a link `result`. A flake of type `path:`
# copies these files. A tree walks the listed folders and leaves them out, so they never enter an output, and a
# tree of a checkout with such files has the same store path as the tree of a clean checkout.
#
#   iso       what the ISO carries as `/etc/basix/source`, and what the installed system reads as its Basix flake
#   library   the Basix library and the `component.py` of each capability (the compiler and the editor launcher)
#   project   the files that the build of the `basix` Python package reads
#
# `nix eval --raw .#lib.sourceTrees.iso` prints the store path of a tree.
{ lib, root }:
let
  # A folder with one of these names is not a source, at any depth.
  junkFolders = [
    "__pycache__"
    ".pytest_cache"
    ".ruff_cache"
    ".mypy_cache"
    ".hypothesis"
    ".venv"
    ".devenv"
    ".direnv"
    ".basix"
    ".git"
    ".jj"
    ".gitman"
    ".testee"
    ".copyroom"
    "node_modules"
  ];
  hasJunkName = name: lib.elem name junkFolders || lib.hasSuffix ".egg-info" name;

  # A file with one of these names is not a source, at any depth.
  isJunkFile = name:
    lib.hasSuffix ".pyc" name
    || lib.hasSuffix ".pyo" name
    || lib.hasSuffix "~" name
    || lib.hasSuffix ".swp" name
    || lib.hasSuffix ".orig" name
    || lib.hasSuffix ".rej" name
    || lib.hasSuffix ".log" name
    || name == ".DS_Store"
    || name == "result"
    || lib.hasPrefix "result-" name;

  # The files below a folder, as a list of paths. A symbolic link is a file (a capability links to its `component.py`).
  filesBelow = keep: dir:
    lib.concatLists (lib.mapAttrsToList
      (name: type:
        let path = dir + "/${name}"; in
        if type == "directory" then (if hasJunkName name then [ ] else filesBelow keep path)
        else if isJunkFile name || !keep name then [ ]
        else [ path ])
      (builtins.readDir dir));

  # A file, or a folder with all its sources (`keep` selects among the file names). A path that does not exist adds
  # nothing: the first commit of a repository has no `CHANGELOG.md` and no `provenance`, and the release job adds both.
  entry = keep: relative:
    let path = root + "/${relative}"; in
    if !builtins.pathExists path then [ ]
    else if builtins.readFileType path == "directory" then filesBelow keep path else [ path ];

  all = _: true;
  componentFile = name: name == "component.py";

  tree = entries: lib.fileset.toSource {
    inherit root;
    fileset = lib.fileset.unions (lib.concatMap (e: entry e.keep e.path) entries);
  };
  whole = path: { inherit path; keep = all; };
  components = { path = "capabilities"; keep = componentFile; };
in
{
  iso = tree (map whole [
    "flake.nix"
    "flake.lock"
    "README.md"
    "CHANGELOG.md"
    "pyproject.toml"
    "uv.lock"
    "capabilities"
    "install"
    "iso"
    "lib"
    "modules"
    "nix"
    "profiles"
    "provenance"
    "src"
    "template"
  ]);

  # `src/basix_config/capabilities/NAME.py` is a symbolic link to `capabilities/NAME/component.py` (010-D10), so both
  # must stay in one tree.
  library = tree [ (whole "src") components ];

  project = tree [ (whole "pyproject.toml") (whole "README.md") (whole "src") components ];
}
