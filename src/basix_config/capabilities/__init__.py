"""The shipped capabilities (009-D17, 010-D10).

Each capability keeps its folder `capabilities/NAME/` at the root of the repository (`component.py`, `module.nix`,
`check.nix`, `lesson.md`). This package reaches `component.py` through a symlink named `NAME.py` (with `_` for `-`).
A test fails on a component with no symlink and on a symlink with no folder.
"""
