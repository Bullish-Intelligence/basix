# desktop: a screen with windows

## What it does

It gives the machine a graphical desktop. Hyprland shows the windows. Noctalia shows the bar. A text login screen asks
for your account name and password. foot is the terminal. Firefox is the browser.

## Turn it on

1. In `system.py`, write `profile=Desktop()` in `Machine`.
2. Run `basix apply`.
3. Restart the machine. The login screen changes at the next start.

After you log in, press SUPER+Q for the terminal and SUPER+B for the browser. SUPER is the key with the logo.

## Settings

- `greeting="Welcome"`: a line of text on the login screen.
- `keyboard_layout="de"`: the keyboard layout of the login screen and the text console.
- `locale="en_US.UTF-8"`: the language. It must be a UTF-8 locale.
- `browser=False`: no Firefox. It is the largest part of the desktop.
- `extra_programs=[Program(pkgs.mpv)]`: more programs for every account.

Basix ships the default look. To change the look of your own account, use `minix` (`Hyprland`, `Noctalia`, `Foot`).

## Turn it off

Write `profile=Console()` and run `basix apply`. Or run `basix undo`.
