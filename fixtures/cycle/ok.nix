# TEST ONLY (BSX-411): the same two services with one order and no loop: b starts before a. systemd finds no cycle.
{ ... }:
{
  systemd.services.ok-a = {
    wantedBy = [ "multi-user.target" ];
    after = [ "ok-b.service" ];
    serviceConfig.ExecStart = "/bin/true";
  };
  systemd.services.ok-b = {
    wantedBy = [ "multi-user.target" ];
    serviceConfig.ExecStart = "/bin/true";
  };
}
