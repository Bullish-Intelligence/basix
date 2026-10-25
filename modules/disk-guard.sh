#!/usr/bin/env bash
# basix-disk-guard (W5 prototype). Read-only: it never writes to a disk.
#   basix-disk-guard check DEVICE [EXPECTED_SIZE_BYTES]
#   basix-disk-guard all DEVICE EXPECTED_SIZE_OR_DASH [DEVICE EXPECTED_SIZE_OR_DASH ...]
# Exit 0 when DEVICE is a blank, unused, non-boot whole disk named by /dev/disk/by-id.
# Exit 1 and print "basix-disk-guard: REFUSED DEVICE: reason" otherwise.
set -u
cmd=${1:-}; shift || true
[ "$cmd" = check ] || [ "$cmd" = all ] || { echo "usage: basix-disk-guard {check DEVICE [SIZE_BYTES]|all DEVICE SIZE_OR_DASH ...}" >&2; exit 2; }

refuse() { echo "basix-disk-guard: REFUSED $dev_arg: $1" >&2; echo "basix-disk-guard: the guard stopped disko before it wrote to the disk" >&2; exit 1; }

check_one() {
dev_arg=$1
want_size=${2:-}
[ -n "$dev_arg" ] || { echo "basix-disk-guard: missing device" >&2; exit 2; }
[ "$want_size" = - ] && want_size=

# findmnt -R PATH stays silent on a live ISO where /mnt is a plain directory, so the
# check would never fire exactly where it matters. List all mounts and prefix-match /mnt.
mnt_mounts=$(findmnt -rn -o TARGET 2>/dev/null | grep -E '^/mnt(/|$)' || true)
[ -z "$mnt_mounts" ] || refuse "a filesystem is mounted under /mnt ($mnt_mounts)"

case "$dev_arg" in
  /dev/disk/by-id/*-part[0-9]*) refuse "is a partition link, not a whole disk" ;;
  /dev/disk/by-id/*) ;;
  *) refuse "is not a /dev/disk/by-id/ path (kernel names move between boots)" ;;
esac
[ -e "$dev_arg" ] || refuse "does not exist on this machine"
dev=$(readlink -f "$dev_arg")
base=$(basename "$dev")

type=$(lsblk -dno TYPE "$dev" 2>/dev/null || true)
[ "$type" = disk ] || refuse "type is '${type:-unknown}', not a disk (optical medium or other)"
[ "$(lsblk -dno RO "$dev")" = 0 ] || refuse "read-only device"

have=$(lsblk -bdno SIZE "$dev")
[ "$have" -ge 64000000000 ] || refuse "size is $have bytes, below the 64 GB minimum"
if [ -n "$want_size" ]; then
  [ "$have" = "$want_size" ] || refuse "size is $have bytes, the layout expects $want_size"
fi

mounts=$(lsblk -rno MOUNTPOINTS "$dev" | tr -d '[:space:]')
[ -z "$mounts" ] || refuse "backs a mounted filesystem ($(lsblk -rno MOUNTPOINTS "$dev" | grep . | tr '\n' ' ')): boot medium, running root, or in use"
while read -r src; do
  [ -b "$src" ] || continue
  if lsblk -rnso NAME "$src" 2>/dev/null | grep -qx "$base"; then
    refuse "backs the mounted filesystem from $src (boot medium or in use)"
  fi
done < <(findmnt -rno SOURCE | sort -u)
if swapon --noheadings --show=NAME 2>/dev/null | while read -r s; do lsblk -rnso NAME "$s" 2>/dev/null; done | grep -qx "$base"; then
  refuse "backs active swap"
fi
[ -z "$(ls -A "/sys/block/$base/holders" 2>/dev/null)" ] || refuse "has holders (device-mapper or md)"

n=$(lsblk -rno NAME "$dev" | tail -n +2 | wc -l)
[ "$n" = 0 ] || refuse "has $n partition(s): not blank"
sig=$(wipefs --noheadings --output TYPE "$dev" 2>/dev/null | tr '\n' ' ')
[ -z "$sig" ] || refuse "has signature(s) $sig: not blank"
if blkid -p "$dev" >/dev/null 2>&1; then refuse "blkid finds a signature: not blank"; fi
echo "basix-disk-guard: OK $dev_arg -> $dev is blank, unused, and not a boot medium" >&2

}
if [ "$cmd" = check ]; then
  [ "$#" -ge 1 ] && [ "$#" -le 2 ] || { echo "basix-disk-guard: check needs DEVICE [SIZE_BYTES]" >&2; exit 2; }
  check_one "$@"
else
  [ "$#" -ge 2 ] && [ $(( $# % 2 )) -eq 0 ] || { echo "basix-disk-guard: all needs DEVICE SIZE_OR_DASH pairs" >&2; exit 2; }
  while [ "$#" -gt 0 ]; do
    check_one "$1" "$2"
    shift 2
  done
fi
