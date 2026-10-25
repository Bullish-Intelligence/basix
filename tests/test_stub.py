"""The stub writer: the text of the managed `flake.nix` (CONCEPT Appendix C, 009-D51 to 009-D53)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pynix_native import Found, discover_source

from basix_config.stub import DEFAULT_BASIX_URL, input_list_expr, make_stub, nix_input_list, write_stub

PLAIN = """\
# Managed by Basix. Do not edit.
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  outputs = { self, basix }: {
    packages.x86_64-linux.compiler = basix.packages.x86_64-linux.compiler;
    nixosConfigurations = basix.lib.machine {
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
    };
  };
}
"""

WITH_INPUT = """\
# Managed by Basix. Do not edit.
{
  inputs.basix.url = "git+https://github.com/Bullish-Intelligence/basix?ref=release";
  inputs.nixpkgs.follows = "basix/nixpkgs";
  inputs.nirinit.url = "github:amaanq/nirinit/REV";
  inputs.nirinit.inputs.nixpkgs.follows = "nixpkgs";
  outputs = inputs@{ self, basix, ... }: {
    packages.x86_64-linux.compiler = basix.packages.x86_64-linux.compiler;
    packages.x86_64-linux.input-packages = basix.lib.inputPackages {
      extraInputs = removeAttrs inputs [ "self" "basix" "nixpkgs" ];
      modules = { nirinit = [ "nirinit" ]; };
    };
    nixosConfigurations = basix.lib.machine {
      compiled = builtins.fromJSON (builtins.readFile ./system.json);
      source = ./system.py;
      root = ./.;
      extraInputs = removeAttrs inputs [ "self" "basix" "nixpkgs" ];
    };
  };
}
"""

COMMIT = "d233902339c02a9c334e7e593de68855ad26c4cb"


def found(*calls: str) -> Found:
    return discover_source("\n".join(calls) + "\n")


def inspect_with(have: dict[str, list[str] | None]):
    """A fake for the read of the inputs of an input."""
    return lambda url: have[url]


def test_the_stub_without_an_input_is_the_text_of_appendix_c() -> None:
    assert make_stub().text == PLAIN
    assert make_stub(found("x = 1")).text == PLAIN
    assert make_stub().notes == ()


def test_the_default_url_is_the_public_one() -> None:
    assert DEFAULT_BASIX_URL == "git+https://github.com/Bullish-Intelligence/basix?ref=release"


def test_the_url_of_basix_is_a_parameter() -> None:
    text = make_stub(basix_url="git+https://example.org/basix?ref=v1").text
    assert 'inputs.basix.url = "git+https://example.org/basix?ref=v1";' in text
    with pytest.raises(ValueError, match="cannot hold"):
        make_stub(basix_url='x"; evil = "')


def test_the_stub_with_an_input() -> None:
    entry = found('Input("nirinit", "github:amaanq/nirinit/REV", modules=["nirinit"])')
    stub = make_stub(entry, inspect=inspect_with({"github:amaanq/nirinit/REV": ["nixpkgs", "systems"]}))
    assert stub.text == WITH_INPUT
    assert stub.notes == ()


def test_no_follows_line_for_an_input_with_no_nixpkgs() -> None:
    """009-D51: a `follows` for a missing input makes Nix warn, so the writer reads the input list first."""
    entry = found('Input("tiny", "github:o/tiny")')
    text = make_stub(entry, inspect=inspect_with({"github:o/tiny": []})).text
    assert 'inputs.tiny.url = "github:o/tiny";\n  outputs' in text
    assert "inputs.tiny.inputs" not in text


def test_own_keeps_the_nixpkgs_of_the_input() -> None:
    entry = found('Input("pytuin", "github:o/pytuin", nixpkgs=OWN)')
    text = make_stub(entry, inspect=inspect_with({"github:o/pytuin": ["nixpkgs"]})).text
    assert "inputs.pytuin.inputs" not in text
    assert 'inputs.nixpkgs.follows = "basix/nixpkgs";' in text  # the layer still has its own follows line


def test_a_nixpkgs_by_commit_adds_an_input_and_a_follows_line() -> None:
    entry = found(
        f'old = NixpkgsPin("old", "{COMMIT}")',
        'Input("nirinit", "github:amaanq/nirinit/REV", nixpkgs=old)',
        'Input("other", "github:o/other")',
    )
    text = make_stub(
        entry, inspect=inspect_with({"github:amaanq/nirinit/REV": ["nixpkgs"], "github:o/other": ["nixpkgs"]})
    ).text
    assert f'  inputs.nixpkgs-old.url = "github:NixOS/nixpkgs/{COMMIT}";\n' in text
    assert '  inputs.nirinit.inputs.nixpkgs.follows = "nixpkgs-old";\n' in text
    assert '  inputs.other.inputs.nixpkgs.follows = "nixpkgs";\n' in text
    # The pins come first, then the inputs in the order of the entry.
    assert text.index("nixpkgs-old.url") < text.index("nirinit.url") < text.index("other.url")


def test_a_pin_with_no_input_still_gets_the_input_form() -> None:
    entry = found(f'old = NixpkgsPin("old", "{COMMIT}")')
    text = make_stub(entry).text
    assert "inputs.nixpkgs-old.url" in text and "extraInputs" in text and "inputs@{ self, basix, ... }" in text


def test_the_input_packages_output_follows_the_inputs_only() -> None:
    """The output `input-packages` is in the stub only when the entry has an `Input` or a `NixpkgsPin`."""
    assert "input-packages" not in make_stub().text
    entry = found(
        f'old = NixpkgsPin("old", "{COMMIT}")',
        'Input("tiny", "github:o/tiny")',
        'Input("src", "github:o/s", flake=False)',
    )
    text = make_stub(entry, inspect=inspect_with({"github:o/tiny": []})).text
    assert "basix.lib.inputPackages {" in text and "      modules = { };\n" in text
    assert make_stub(found(f'old = NixpkgsPin("old", "{COMMIT}")')).text.count("input-packages") == 1


def test_a_plain_source_folder() -> None:
    entry = found('Input("src", "github:o/src", flake=False)')
    stub = make_stub(entry, inspect=lambda url: pytest.fail("a source folder has no input list"))
    assert 'inputs.src.url = "github:o/src";\n  inputs.src.flake = false;\n' in stub.text
    assert "inputs.src.inputs" not in stub.text


def test_notes_name_the_line_of_the_input() -> None:
    entry = found(
        f'old = NixpkgsPin("old", "{COMMIT}")',
        'Input("tiny", "github:o/tiny", nixpkgs=old)',
        'Input("blind", "github:o/blind")',
    )
    stub = make_stub(entry, inspect=inspect_with({"github:o/tiny": [], "github:o/blind": None}))
    assert stub.notes == (
        "system.py:2: the input `tiny` has no `nixpkgs` input, so `nixpkgs=old` changes nothing for it",
        "system.py:3: could not read the inputs of `blind`, so the stub has no follows line for it",
    )


def test_write_stub(tmp_path: Path) -> None:
    stub = write_stub(tmp_path)
    assert (tmp_path / "flake.nix").read_text() == stub.text == PLAIN
    assert stub.text.splitlines()[0] == "# Managed by Basix. Do not edit."


# PF-6: the list of inputs of an input honours `dir=`.

EXAMPLE_URL = (
    "git+https://github.com/example/example?dir=nix/example-host&ref=main&rev=a1de8f0c0000000000000000000000000000000a"
)


@pytest.mark.parametrize(
    "url",
    [
        EXAMPLE_URL,
        "github:o/r?dir=sub",
        "github:o/r/REV",
        "path:/x/y?dir=a/b",
        "git+file:///x/y?dir=sub&rev=0123456789abcdef0123456789abcdef01234567",
    ],
)
def test_the_input_list_expression_reads_the_flake_in_dir(url: str) -> None:
    expr = input_list_expr(url)
    # The reference goes whole to the parser, so `ref`, `rev`, and the other parts stay.
    assert f'builtins.parseFlakeRef "{url}"' in expr
    # `fetchTree` gets the reference without `dir`, and the read goes to `DIR/flake.nix`.
    assert 'builtins.fetchTree (builtins.removeAttrs ref [ "dir" ])' in expr
    assert '"/${ref.dir or "."}/flake.nix"' in expr
    # The old expression read `ROOT/flake.nix` and ignored `dir`.
    assert 'src + "/flake.nix"' not in expr
    assert '+ "/flake.nix"' not in expr


def test_the_input_list_reads_a_flake_in_a_subdirectory(tmp_path: Path) -> None:
    if shutil.which("nix") is None or shutil.which("git") is None:
        pytest.skip("needs nix and git")
    repo = tmp_path / "repo"
    (repo / "nix" / "sub").mkdir(parents=True)
    (repo / "nix" / "sub" / "flake.nix").write_text(
        '{ inputs.nixpkgs.url = "github:NixOS/nixpkgs"; inputs.other.url = "github:o/o"; outputs = _: { }; }\n'
    )
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.org",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.org",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "x")
    rev = git("rev-parse", "HEAD")
    got = nix_input_list(f"git+file://{repo}?dir=nix/sub&rev={rev}")
    assert got is not None
    assert sorted(got) == ["nixpkgs", "other"]


def test_the_default_channel_is_the_branch_release() -> None:
    # 010-D54: `release` follows v2. The alias `release-v2` is not the default.
    assert DEFAULT_BASIX_URL.endswith("?ref=release")
