"""`basix update [NAME]`: move the lock, print the names, the age of each nixpkgs, and the changelog (009-D10, D54)."""

import json
from pathlib import Path

import pytest
from pynix_engine import Result
from test_cmd_support import CHANGELOG, DAY, NIX, NOW, Setup, initialized, lock_json, locked_of

from basix import commands
from basix.release import changelog_sections, nixpkgs_ages, relevant_lines, source_expr, source_tag, tag_name
from basix_config.stub import DEFAULT_BASIX_URL, make_stub

NEW_LOCK = lock_json(nar_hash="sha256-BBBB", rev="c" * 40, pynix_tag="pynix-v0.1.0-rc.3", pynix_rev="q" * 40)
SERVICE = {
    "path": ["services", "openssh"],
    "source": "system.py:5 (RemoteLogin)",
    "value": {"enable": True, "settings": {"PermitRootLogin": "no"}},
    "version": 1,
}


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    setup = initialized(tmp_path, update_to=NEW_LOCK)
    (setup.ws / "system.json").write_text(json.dumps({"name": "pybox", "fragments": [SERVICE]}))
    return setup


def test_update_prints_the_names_the_age_and_the_changelog(setup: Setup) -> None:
    assert commands.update(setup.ctx) == 0, setup.lines
    assert setup.lines == [
        "Basix v0.1.0 -> v0.1.1",
        "nixpkgs e7439b6b, 13 days old",
        "pynix-v0.1.0-rc.3 changes options that system.py sets:",
        "  services.openssh.settings.PermitRootLogin: bool -> enum  [RemoteLogin]",
        "next: run basix apply to use it",
    ]
    assert (setup.ws / "flake.lock").read_text() == NEW_LOCK


def test_update_uses_the_provenance_key_to_name_the_field_or_capability(setup: Setup) -> None:
    path = "services.openssh.settings.PermitRootLogin"
    (setup.ws / "system.json").write_text(
        json.dumps({"name": "pybox", "fragments": [SERVICE], "provenance": {"RemoteLogin": [path], "name": ["x.y"]}})
    )
    assert commands.update(setup.ctx) == 0, setup.lines
    assert f"  {path}: bool -> enum  [RemoteLogin]" in setup.lines


def test_update_says_when_no_change_touches_the_workspace(setup: Setup) -> None:
    (setup.ws / "system.json").write_text(json.dumps({"name": "pybox", "fragments": []}))
    assert commands.update(setup.ctx) == 0
    assert "pynix-v0.1.0-rc.2 -> pynix-v0.1.0-rc.3: no change to an option that system.py sets" in setup.lines


def test_update_names_every_tag_between_the_old_and_the_new(setup: Setup) -> None:
    text = "# Changelog\n\n## t3\n\n```text\nservices.openssh.enable: a -> b\n```\n\n## t2\n\n```text\nservices.openssh.enable: c -> d\n```\n\n## t1\n\n```text\nservices.openssh.enable: e -> f\n```\n"
    sections = changelog_sections(text, "t1")
    assert [tag for tag, _ in sections] == ["t3", "t2"]
    assert changelog_sections(text, "gone") == sections[:1]  # an old tag that the file lacks: the newest only
    assert changelog_sections(text, None) == sections[:1]


def test_update_with_no_new_release_says_so_and_prints_no_changelog(tmp_path: Path) -> None:
    setup = initialized(tmp_path, update_to=lock_json())
    setup.world.new_version = "0.1.0"
    assert commands.update(setup.ctx) == 0
    assert setup.lines == [
        "Basix is up to date (v0.1.0)",
        "nixpkgs e7439b6b, 13 days old",
        "next: run basix apply to use it",
    ]


def test_update_adds_the_revision_when_two_releases_share_a_name(tmp_path: Path) -> None:
    setup = initialized(tmp_path, update_to=NEW_LOCK)
    setup.world.new_version = "0.1.0"
    assert commands.update(setup.ctx) == 0
    assert setup.lines[0] == f"Basix v0.1.0 ({'a' * 7}) -> v0.1.0 ({'c' * 7})"


PATH_URL = "path:/nix/store/zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-source"
MOVED = "This machine used the copy of Basix from the installer. It now follows the release channel."


def stub_with(setup: Setup, url: str) -> Path:
    stub = setup.ws / "flake.nix"
    stub.write_text(stub.read_text().replace(DEFAULT_BASIX_URL, url))
    return stub


def test_update_moves_a_path_stub_to_the_release_channel_and_says_so(tmp_path: Path) -> None:
    """011 bug 2: a fresh install holds `path:`, so a machine never followed the channel."""
    setup = initialized(tmp_path, update_to=NEW_LOCK)
    stub = stub_with(setup, PATH_URL)
    assert commands.update(setup.ctx) == 0, setup.lines
    assert setup.lines[0] == MOVED
    assert stub.read_text() == make_stub().text
    assert setup.lines.count(MOVED) == 1


def test_update_gives_the_old_path_stub_back_when_the_update_fails(tmp_path: Path) -> None:
    setup = initialized(tmp_path)  # update_to is None: nix fails
    stub = stub_with(setup, PATH_URL)
    before = stub.read_text()
    assert commands.update(setup.ctx) == 2
    assert stub.read_text() == before
    assert "The update failed. The lock did not change." in setup.lines[-1]


def test_update_keeps_a_stub_that_already_follows_a_channel(setup: Setup) -> None:
    stub = stub_with(setup, "git+https://github.com/example.org/mine?ref=release")
    before = stub.read_text()
    assert commands.update(setup.ctx) == 0
    assert stub.read_text() == before
    assert MOVED not in setup.lines


def test_update_of_another_input_leaves_a_path_stub_alone(tmp_path: Path) -> None:
    setup = initialized(tmp_path, update_to=lock_json(nar_hash="sha256-BBBB", inputs=["nirinit"]))
    stub = stub_with(setup, PATH_URL)
    assert commands.update(setup.ctx, "nirinit") == 0, setup.lines
    assert PATH_URL in stub.read_text()
    assert MOVED not in setup.lines


def test_apply_keeps_a_path_stub_so_the_first_apply_works_offline(tmp_path: Path) -> None:
    setup = initialized(tmp_path)
    stub = stub_with(setup, PATH_URL)
    assert commands.apply(setup.ctx) == 0, setup.lines
    assert PATH_URL in stub.read_text()


def test_update_runs_one_nix_update_for_basix(setup: Setup) -> None:
    assert commands.update(setup.ctx) == 0
    assert setup.nix_calls("flake", "update") == [(*NIX, "flake", "update", "basix", "--no-warn-dirty")]


def test_update_does_not_save_or_switch(setup: Setup) -> None:
    assert commands.update(setup.ctx) == 0
    assert setup.commits() == ["install"]
    assert setup.sudo_calls() == []
    assert not [t for t in setup.trace() if t.startswith("test") or t == "build"]


def test_update_of_an_unknown_input_stops_and_keeps_the_lock(tmp_path: Path) -> None:
    setup = initialized(tmp_path)  # update_to is None: nix fails
    before = (setup.ws / "flake.lock").read_text()
    assert commands.update(setup.ctx, "nope") == 2
    assert setup.lines == ["The update failed. The lock did not change.\nerror: input 'nope' does not exist"]
    assert (setup.ws / "flake.lock").read_text() == before


def test_update_of_an_input_makes_the_input_packages_again(tmp_path: Path) -> None:
    setup = initialized(tmp_path, update_to=lock_json(nar_hash="sha256-BBBB", inputs=["nirinit"]))
    (setup.ws / "system.py").write_text(
        'inputs = [Input("nirinit", "github:amaanq/nirinit/REV", modules=["nirinit"])]\n'
    )
    assert commands.update(setup.ctx, "nirinit") == 0, setup.lines
    assert setup.nix_calls("flake", "update") == [(*NIX, "flake", "update", "nirinit", "--no-warn-dirty")]
    assert [c for c in setup.nix_calls("build") if c[-1] == ".#input-packages"]
    assert setup.trace().index("update") < setup.trace().index("packages")
    assert setup.lines[0].startswith("nirinit ")


def test_update_without_inputs_builds_no_packages(setup: Setup) -> None:
    assert commands.update(setup.ctx) == 0
    assert [c for c in setup.nix_calls("build") if c[-1] == ".#input-packages"] == []


def test_update_leaves_a_system_py_that_cannot_be_read_to_the_next_apply(setup: Setup) -> None:
    (setup.ws / "system.py").write_text("machine = (\n")
    assert commands.update(setup.ctx) == 0
    assert setup.lines[-1] == "next: run basix apply to use it"


def test_update_outside_a_workspace_says_where_to_go(setup: Setup) -> None:
    setup.ctx.cwd = setup.home / "nowhere"
    (setup.ws / ".git").rename(setup.ws / ".gone")
    assert commands.update(setup.ctx) == 1
    assert setup.lines[0].endswith("is not a basix workspace. Go to ~/basix first.")


def test_update_waits_when_another_basix_command_runs(setup: Setup) -> None:
    import fcntl

    (setup.ws / ".git" / "basix").mkdir(exist_ok=True)
    with (setup.ws / ".git" / "basix" / "lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert commands.update(setup.ctx) == 1
    assert setup.lines == ["Another basix command runs in this workspace. Wait for it."]
    assert setup.nix_calls("flake", "update") == []


# --- the parts ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("ref", "name"),
    [("refs/tags/v0.2.0", "v0.2.0"), ("v1.2", "v1.2"), ("release", None), ("main", None), (None, None)],
)
def test_tag_name(ref: str | None, name: str | None) -> None:
    assert tag_name(ref) == name


def lock_with(*pins: tuple[str, str, int | None]) -> dict:
    nodes: dict = {"root": {"inputs": {name: name for name, _, _ in pins}}}
    for name, rev, stamp in pins:
        nodes[name] = {"locked": {"rev": rev, **({"lastModified": stamp} if stamp is not None else {})}}
    return {"root": "root", "nodes": nodes}


def test_the_age_of_each_pinned_nixpkgs() -> None:
    lock = lock_with(("nixpkgs", "e7439b6b14ad", int(NOW) - 13 * DAY), ("nixpkgs-old", "1234567890ab", int(NOW) - DAY))
    assert nixpkgs_ages(lock, NOW) == ["nixpkgs e7439b6b, 13 days old", "nixpkgs-old 12345678, 1 day old"]


def test_the_age_has_no_limit_and_a_pin_with_no_date_says_so() -> None:
    lock = lock_with(("nixpkgs", "e7439b6b14ad", int(NOW) - 900 * DAY), ("nixpkgs-x", "abcdefabcdef", None))
    assert nixpkgs_ages(lock, NOW) == ["nixpkgs e7439b6b, 900 days old", "nixpkgs-x abcdefab, age unknown"]


def test_a_pin_inside_the_basix_node_has_an_age_too() -> None:
    lock = json.loads(lock_json())
    assert nixpkgs_ages(lock, NOW) == ["nixpkgs e7439b6b, 13 days old"]


def test_the_filter_keeps_the_lines_that_name_an_option_of_the_workspace() -> None:
    body = changelog_sections(CHANGELOG, "pynix-v0.1.0-rc.2")[0][1]
    paths = {"RemoteLogin": ["services.openssh.settings.PermitRootLogin"], "Machine": ["boot.loader.timeout"]}
    assert relevant_lines(body, paths) == [
        "services.openssh.settings.PermitRootLogin: bool -> enum  [RemoteLogin]",
        "boot.loader.timeout: int -> str  [Machine]",
    ]


def test_a_group_name_in_the_changelog_covers_the_options_below_it() -> None:
    body = [" Removed (3 options, 1 subtrees): services.openssh 3"]
    assert relevant_lines(body, {"RemoteLogin": ["services.openssh.enable"]}) == [
        "Removed (3 options, 1 subtrees): services.openssh 3  [RemoteLogin]"
    ]
    assert relevant_lines(body, {"X": ["services.nginx.enable"]}) == []


# --- the release name: the tag of provenance/inputs.json, then the version, then the ref (010-D31) -----
def serve_tags(setup: Setup, tmp_path: Path, tags: tuple[str | None, str | None], version: str = "0.1.0") -> None:
    """Make the fetch of a locked source give a Basix source for each side of the update.

    `tags` is the `tag` key of `provenance/inputs.json` before and after; None leaves the file out.
    """

    def fetch(argv):
        side = 1 if setup.world.updated else 0
        source = tmp_path / f"basix-source-{side}"
        (source / "provenance").mkdir(parents=True, exist_ok=True)
        (source / "pyproject.toml").write_text(f'[project]\nname = "basix"\nversion = "{version}"\n')
        if tags[side]:
            (source / "provenance" / "inputs.json").write_text(json.dumps({"tag": tags[side], "pynix": {}}))
        return Result(0, "warning: noise\n" + json.dumps(str(source)) + "\n")

    setup.fake.on(NIX + ("eval", "--json", "--expr"), fetch)


def test_update_names_two_releases_by_the_tag_of_inputs_json(tmp_path: Path) -> None:
    setup = initialized(tmp_path, update_to=NEW_LOCK)
    serve_tags(setup, tmp_path, ("v0.2.0-rc.1", "v0.2.0-rc.2"))
    assert commands.update(setup.ctx) == 0, setup.lines
    assert setup.lines[0] == "Basix v0.2.0-rc.1 -> v0.2.0-rc.2"


def test_update_tells_two_rcs_of_one_version_apart(tmp_path: Path) -> None:
    """Both sources hold the same `version` in `pyproject.toml`; only the recorded tag differs."""
    setup = initialized(tmp_path, update_to=NEW_LOCK)
    serve_tags(setup, tmp_path, ("v0.1.0-rc.1", "v0.1.0-rc.2"))
    assert commands.update(setup.ctx) == 0, setup.lines
    assert setup.lines[0] == "Basix v0.1.0-rc.1 -> v0.1.0-rc.2"
    assert "(" not in setup.lines[0]


def test_update_falls_back_to_the_version_when_inputs_json_has_no_tag(tmp_path: Path) -> None:
    setup = initialized(tmp_path, update_to=NEW_LOCK)
    serve_tags(setup, tmp_path, ("v0.3.0", None))
    assert commands.update(setup.ctx) == 0, setup.lines
    assert setup.lines[0] == "Basix v0.3.0 -> v0.1.0"


def test_update_falls_back_to_the_ref_when_the_source_is_unknown(tmp_path: Path) -> None:
    setup = initialized(tmp_path, update_to=NEW_LOCK)
    setup.fake.on(NIX + ("eval", "--json", "--expr"), lambda argv: Result(1, "error: cannot fetch\n"))
    assert commands.update(setup.ctx) == 0, setup.lines
    assert setup.lines[0] == f"Basix {'a' * 7} -> {'c' * 7}"


def test_update_reads_each_release_from_its_own_lock_and_never_archives_the_stub(tmp_path: Path) -> None:
    """011 bug 1: the stub already named the new channel, so an archive of it gave the new source for the old lock."""
    setup = initialized(tmp_path, update_to=NEW_LOCK)
    tags = {"a" * 40: "v0.2.0", "c" * 40: "v0.3.0"}

    def fetch(argv):
        locked = locked_of(argv[-1])
        source = tmp_path / f"source-{locked['rev'][:1]}"
        (source / "provenance").mkdir(parents=True, exist_ok=True)
        (source / "provenance" / "inputs.json").write_text(json.dumps({"tag": tags.get(locked["rev"], "pynix")}))
        return Result(0, json.dumps(str(source)) + "\n")

    setup.fake.on(NIX + ("eval", "--json", "--expr"), fetch)
    stub = setup.ws / "flake.nix"
    stub.write_text(stub.read_text().replace("?ref=release", "?ref=release-v2"))  # the stub moved before the update
    assert commands.update(setup.ctx) == 0, setup.lines
    assert setup.lines[0] == "Basix v0.2.0 -> v0.3.0"
    assert setup.nix_calls("flake", "archive") == []
    assert [locked_of(call[-1])["rev"] for call in setup.nix_calls("eval", "--json", "--expr")][:2] == [
        "a" * 40,
        "c" * 40,
    ]


def test_source_expr_names_the_locked_attributes_and_none_for_a_missing_node() -> None:
    lock = json.loads(lock_json())
    assert locked_of(source_expr(lock, "basix") or "")["rev"] == "a" * 40
    assert locked_of(source_expr(lock, "basix", "pynix") or "")["narHash"] == "sha256-PYNIX"
    assert source_expr(lock, "nope") is None
    assert source_expr({}, "basix") is None


@pytest.mark.parametrize("text", ["not json", "[]", '{"tag": 3}', '{"tag": ""}', "{}"])
def test_source_tag_ignores_a_file_that_holds_no_tag(tmp_path: Path, text: str) -> None:
    (tmp_path / "provenance").mkdir()
    (tmp_path / "provenance" / "inputs.json").write_text(text)
    assert source_tag(str(tmp_path)) is None
    assert source_tag(str(tmp_path / "nowhere")) is None
    assert source_tag(None) is None


def test_source_tag_reads_the_tag(tmp_path: Path) -> None:
    (tmp_path / "provenance").mkdir()
    (tmp_path / "provenance" / "inputs.json").write_text('{"tag": "v0.1.0-rc.7", "version": "0.1.0"}')
    assert source_tag(str(tmp_path)) == "v0.1.0-rc.7"
