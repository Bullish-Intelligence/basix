# TEST ONLY (BSX-411): two services start at boot, and each starts after the other. systemd finds a cycle.
{ ... }:
{
  systemd.services.cycle-a = {
    wantedBy = [ "multi-user.target" ];
    after = [ "cycle-b.service" ];
    serviceConfig.ExecStart = "/bin/true";
  };
  systemd.services.cycle-b = {
    wantedBy = [ "multi-user.target" ];
    after = [ "cycle-a.service" ];
    serviceConfig.ExecStart = "/bin/true";
  };
}
