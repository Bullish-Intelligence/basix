# Remove hardware identifiers that can identify a person or a device.
# A disk name under /dev/disk/by-id, by-uuid, by-label, or by-partuuid holds a serial number, a UUID, or a label.
def disk_name: test("/dev/disk/by-(id|uuid|label|partuuid)/");
# First pass: drop such names from lists. The second pass would turn them into null and keep them.
def strip_lists:
  walk(if type == "array" then map(select((type == "string" and disk_name) | not)) else . end);
def strip_ids:
  walk(
    if type == "object" then
      with_entries(
        select((.key | test("(?i)(serial|wwn)") | not) and (.key | disk_name | not))
      )
    elif type == "string" and disk_name then
      null
    else
      .
    end
  );
strip_lists | strip_ids | del(.hardware.cpu[].tlb_size)
