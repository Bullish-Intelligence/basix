# Your Basix workspace

This folder holds the settings of your machine. Each change goes in one place.

## Where a change goes

- `home.py`: your programs and their settings. Start here for a program that you run as yourself.
- `system.py`: the machine itself. It holds the machine name, your account, the disk, the networks you trust,
  open ports, and capabilities. A capability is a function that you can switch on, such as `RemoteLogin()`.
  A program for every account of the machine goes here too: `programs=[Program(pkgs.htop)]`. `system.py` names the disk by its `/dev/disk/by-id/` name, which holds the serial number of the disk.
- When a setting has no field in `system.py`, use the hatches in this order: a field, then `options=[...]` with a
  NixOS class (`nixos.NAME(...)`), then `native_modules=[NativeModule("fans.nix")]` with a `.nix` file of this folder.

Your editor completes the names and shows the rule of each field: start `nix run --inputs-from . basix#editor` as its
language server. A mistake gives one line with the file, the line, and the column.

## Commands

Run these in this folder, or set `BASIX_WORKSPACE` to it. Type your password when `sudo` asks.

- `basix apply`: build the new system from your files and switch to it.
- `basix undo`: go back to the system before the last apply, with the files of that time.
- `basix update`: take the newest Basix. Run `basix apply` after it.
- `basix explain`: show where a setting comes from.

## Files that Basix writes

Do not edit these. `basix apply` writes them again.

- `flake.nix`, `flake.lock`: the pins of the versions you use.
- `system.json`: your `system.py`, compiled.
- `facter.json`: the hardware report that the install made.

## Settings that have no file

These change the machine at once. `basix apply` is not needed and `basix undo` does not undo them.

- `timedatectl`: time and time zone.
- `localectl`: keyboard and language.
- `nmtui`: Wi-Fi and other networks.
