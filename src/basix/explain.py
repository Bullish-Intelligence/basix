"""`basix explain NAME`: show the value of a setting and the line of `system.py` that sets it (009-D14).

NAME is a field of `Machine` (`trusted_interfaces`), a capability (`RemoteLogin`), or, in the advanced form, an option
path (`services.openssh.ports`). `system.json` holds the key `provenance`: the option paths that each field and
capability set. One `nix eval` call asks the machine of the workspace for the value of each path and for the files that
define it. The command maps each file back to a line of `system.py`. It prints no priority word.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any

from pynix_engine import Runner, trim_nix_error

__all__ = ["explain"]

#: A learner definition: the `_file` of a fragment is `system.py:7 (RemoteLogin)`.
LEARNER = re.compile(r"^(?P<site>[^/\s]+:\d+) \((?P<label>[^)]+)\)$")
#: A file of the Nix store, and the path below the store folder.
STORE = re.compile(r"^/nix/store/[a-z0-9]{32}-[^/]+/(?P<below>.+)$")
#: The words of an option path: the characters that a Nix attribute name needs here. Nothing else reaches Nix.
OPTION_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_'-]*(\.[A-Za-z0-9_][A-Za-z0-9_'-]*)*$")
#: The most files that one line of output names before it counts them.
FILES_SHOWN = 3
#: The most list items that a value shows.
ITEMS_SHOWN = 8
#: The longest text of a value.
VALUE_WIDTH = 100
#: The most child names that the line for a prefix of options shows.
CHILDREN_SHOWN = 6

#: The Nix function that `nix eval --apply` runs on the machine. `PATHS` is the JSON text of the list of option paths.
#: For each path it finds the longest prefix that is an option, takes the value from `config`, and takes the files from
#: `definitionsWithLocations` of the option (only the winning definitions). A path below an option with free names
#: (`services.openssh.settings.AllowUsers`) keeps the definitions that hold the rest of the path. A path with no option
#: gets the names that stand beside it, for "did you mean". A path that is a prefix of options gets the names below it.
EXPRESSION = """\
m:
let
  lib = m.pkgs.lib;
  paths = builtins.fromJSON "PATHS";
  show = d: v:
    if builtins.isFunction v then "<function>"
    else if lib.isDerivation v then (v.name or "<package>")
    else if builtins.isAttrs v then (if d == 0 then "{...}" else builtins.mapAttrs (_: show (d - 1)) v)
    else if builtins.isList v then (if d == 0 then [ "..." ] else map (show (d - 1)) v)
    else v;
  safe = f: let r = builtins.tryEval (builtins.deepSeq f f); in { ok = r.success; value = if r.success then r.value else null; };
  declared = parts:
    let
      go = n:
        if n == 0 then null
        else
          let pre = lib.take n parts; o = lib.attrByPath pre null m.options;
          in if o != null && lib.isOption o then { path = pre; option = o; } else go (n - 1);
    in go (builtins.length parts);
  beside = parts:
    let
      go = n:
        let pre = lib.take n parts; o = lib.attrByPath pre null m.options;
        in if n == 0 || (builtins.isAttrs o && !(lib.isOption o)) then { prefix = pre; names = builtins.filter (s: !(lib.hasPrefix "_" s)) (builtins.attrNames (if n == 0 then m.options else o)); }
           else go (n - 1);
    in go (builtins.length parts - 1);
  one = p:
    let
      parts = lib.splitString "." p;
      found = declared parts;
      rest = lib.drop (builtins.length found.path) parts;
      hasRest = d: rest == [ ] || (builtins.isAttrs d.value && lib.hasAttrByPath rest d.value);
      got = safe (show 4 (lib.attrByPath parts null m.config));
    in
    if found == null then
      let
        below = lib.attrByPath parts null m.options;
        children = builtins.filter (s: !(lib.hasPrefix "_" s)) (builtins.attrNames below);
      in
      { name = p; known = false; near = beside parts; }
      // lib.optionalAttrs (builtins.isAttrs below && !(lib.isOption below) && children != [ ]) { inherit children; }
    else {
      name = p;
      known = true;
      option = lib.concatStringsSep "." found.path;
      shown = got.ok;
      value = got.value;
      files = map (d: d.file) (builtins.filter hasRest found.option.definitionsWithLocations);
    };
in
map one paths
"""


def _nix_string(text: str) -> str:
    """`text` as the body of a Nix string literal (without the quotes)."""
    body = json.dumps(text, ensure_ascii=False)[1:-1]  # the JSON escapes `\\`, `"`, and the new line like Nix does
    return body.replace("${", "\\${")


def expression(paths: list[str]) -> str:
    """The text of the `--apply` function for these option paths."""
    return EXPRESSION.replace("PATHS", _nix_string(json.dumps(paths, ensure_ascii=False)))


def _is_field(name: str) -> bool:
    """A field of `Machine` has a lower-case name. A capability or a profile is a class."""
    return name[:1].islower()


def _capabilities() -> list[str]:
    """The capability names that the library offers (empty when the library is not in reach)."""
    try:
        import basix_config
        from basix_config.component import Capability
    except ImportError:
        return []
    return sorted(
        n
        for n in basix_config.__all__
        if isinstance(getattr(basix_config, n, None), type) and issubclass(getattr(basix_config, n), Capability)
    )


def _close(word: str, names: list[str]) -> str | None:
    found = difflib.get_close_matches(word, names, n=1)
    return found[0] if found else None


def _unknown(name: str, known: list[str]) -> int:
    near = _close(name, known)
    advice = f" Did you mean `{near}`?" if near else f" The names are: {', '.join(sorted(known))}."
    print(f"`{name}` is not a field, a capability, or an option path of this machine.{advice}", file=sys.stderr)
    return 1


def _value(item: dict[str, Any]) -> str:
    if not item.get("shown"):
        return "(Nix cannot show it)"
    value = item["value"]
    if isinstance(value, list) and len(value) > ITEMS_SHOWN:
        head = ", ".join(json.dumps(v, ensure_ascii=False) for v in value[:ITEMS_SHOWN])
        return f"[{head}, ... {len(value) - ITEMS_SHOWN} more]"
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= VALUE_WIDTH else text[: VALUE_WIDTH - 3] + "..."


def _owners(site_label: str, leaf: str, provenance: dict[str, list[str]]) -> str:
    """The name that the learner knows: the field of `Machine` that set the path, or the label of the capability."""
    if site_label != "Machine":
        return site_label

    def covers(path: str) -> bool:
        return path == leaf or path.startswith(leaf + ".") or leaf.startswith(path + ".")

    fields = [k for k, paths in provenance.items() if _is_field(k) and any(covers(p) for p in paths)]
    return ", ".join(fields) if fields else "Machine"


def lines_for(item: dict[str, Any], provenance: dict[str, list[str]]) -> list[str]:
    """The text of one option path: the value, then one line for each definition."""
    leaf = item["name"]
    out = [f"{leaf} = {_value(item)}"]
    seen: list[str] = []
    basix = False
    nixos: list[str] = []
    other: list[str] = []
    for file in dict.fromkeys(item["files"]):  # one file can define the option twice
        learner = LEARNER.match(file)
        store = STORE.match(file)
        below = store["below"] if store else file
        if learner:
            seen.append(f"set by {_owners(learner['label'], leaf, provenance)} at {learner['site']}")
        elif below.startswith(("modules/", "profiles/", "capabilities/")):
            basix = True
        elif below.startswith("nixos/modules/"):
            nixos.append(below.removeprefix("nixos/modules/"))
        else:
            other.append(below)
    out += [f"  {line}" for line in dict.fromkeys(seen)]
    if basix:
        out.append("  set by Basix")
    if len(nixos) > FILES_SHOWN:
        out.append(f"  set by NixOS ({len(nixos)} modules)")
    else:
        out += [f"  set by NixOS ({file})" for file in nixos]
    out += [f"  set by the module {file}" for file in other]
    if not item["files"]:
        out.append("  no line sets it: the value is the default")
    return out


def _missing(item: dict[str, Any]) -> str:
    """The line for an option path that no option holds."""
    path = item["name"]
    children = sorted(item.get("children") or [])
    if children:  # the path is a prefix of options, not an option: say so and list the names below it
        shown = ", ".join(f"`{path}.{c}`" for c in children[:CHILDREN_SHOWN])
        more = f", and {len(children) - CHILDREN_SHOWN} more" if len(children) > CHILDREN_SHOWN else ""
        return f"`{path}` is not an option of this machine. It holds these options: {shown}{more}."
    parts = path.split(".")
    prefix = item["near"]["prefix"]
    word = parts[len(prefix)] if len(parts) > len(prefix) else parts[-1]
    near = _close(word, item["near"]["names"])
    suggestion = ".".join([*prefix, near]) if near else None
    advice = f" Did you mean `{suggestion}`?" if suggestion and suggestion != path else ""  # never repeat the name
    return f"`{path}` is not an option of this machine.{advice}"


def _evaluate(folder: Path, machine: str, paths: list[str], runner: Runner) -> list[dict[str, Any]] | str:
    """One `nix eval` call for all paths. Returns the answers, or the text of the Nix error."""
    argv = [
        "nix",
        "--extra-experimental-features",
        "nix-command flakes",
        "eval",
        "--json",
        f".#nixosConfigurations.{machine}",
        "--apply",
        expression(paths),
    ]
    done = runner.run(argv, cwd=folder)
    if done.code != 0:
        return trim_nix_error(done.out)
    for line in reversed(done.out.splitlines()):  # warnings of Nix stand before the answer, which is one line
        if line.startswith("["):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return "Nix gave no answer."


def explain(folder: Path, name: str, *, runner: Runner) -> int:
    """Print the value of NAME and the line that sets it. Returns the exit code (0, or 1 for a mistake)."""
    compiled_file = folder / "system.json"
    try:
        compiled = json.loads(compiled_file.read_text())
        machine, provenance = compiled["name"], compiled["provenance"]
    except OSError:
        print("There is no system.json yet. Run `basix apply`, then `basix explain`.", file=sys.stderr)
        return 1
    except (ValueError, KeyError):
        print("system.json holds no list of settings. Run `basix apply` again.", file=sys.stderr)
        return 1
    known = sorted({*provenance, *_capabilities()})
    if name in provenance:
        paths: list[str] = provenance[name]
        if not paths:
            print(f"`{name}` sets no option on this machine.")
            return 0
    elif name in known:
        print(f"`{name}` is not in the `capabilities` of this machine.", file=sys.stderr)
        return 1
    elif OPTION_PATH.match(name) and ("." in name or not _close(name, known)):
        paths = [
            name
        ]  # a word with a close field or capability is a typo; any other word may be an option such as `boot`
    else:
        return _unknown(name, known)
    answer = _evaluate(folder, machine, paths, runner)
    if isinstance(answer, str):
        print(answer, file=sys.stderr)
        return 1
    code = 0
    for item in answer:
        if item["known"]:
            print("\n".join(lines_for(item, provenance)))
        else:
            print(_missing(item), file=sys.stderr)
            code = 1
    return code
