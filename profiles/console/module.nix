# The profile `console` (BSX-192): the core machine and nothing more.
#
# The core (`modules/core.nix`) gives the text console, NetworkManager, zram, one account, and no SSH
# server. The entry module imports the core for every machine, so this profile adds no option.
# A later profile (for example a desktop) adds its parts here. The profile has no lesson file; the
# core lessons L0 and L1 serve it (NAMES, CURRICULUM).
{ }
