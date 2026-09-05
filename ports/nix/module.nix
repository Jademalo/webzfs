{ config, lib, pkgs, ... }:

let
  cfg = config.services.webzfs;
  webzfsDir = "${cfg.package}/opt/webzfs";
in
{
  options.services.webzfs = {
    enable = lib.mkEnableOption "WebZFS - Web-based ZFS management interface";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.webzfs";
      description = ''
        The WebZFS package to use.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 26619;
      description = "Port to listen on.";
    };

    host = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = "Host address to bind to.";
    };

    settings = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = {
        SECRET_KEY = "your-secret-key-here";
        AUTH_SESSION_EXPIRES_SECONDS = "3600";
      };
      description = "Additional environment variables for WebZFS.";
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Whether to open the firewall for the WebZFS port.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "webzfs";
      description = "User account to run WebZFS as.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "webzfs";
      description = "Group for the WebZFS user.";
    };
  };

  config = lib.mkIf cfg.enable {

    # Enable ZFS filesystem support
    boot.supportedFilesystems = [ "zfs" ];
    # Enable Sanoid
    service.sanoid.enable = true;

    users.users.${cfg.user} = {
      isSystemUser = true;
      group = cfg.group;
      description = "WebZFS service user";
      extraGroups = [
        # WebZFS's web login authenticates against PAM as this unprivileged
        # user.  pam_unix delegates password verification for a non-root
        # caller to the setuid `unix_chkpwd` helper, which deliberately only
        # ever reads /etc/shadow with the *caller's* credentials -- so the
        # service user must be able to read it to verify arbitrary accounts.
        # NixOS already ships the `shadow` group with /etc/shadow mode
        # 0640 root:shadow; joining it is all that is needed.  Without this,
        # all logins fail with "pam_unix(login:auth): authentication failure"
        # despite the correct password.
        "shadow"
        # Read access to the systemd journal for the log readout, so
        # `journalctl` works without sudo.
        "systemd-journal"
        # Raw block device read access so `blkid` works without sudo.
        "disk"
      ];
    };

    users.groups.${cfg.group} = { };


    systemd.services.webzfs = {
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "zfs-mount.service" ];

       path = with pkgs; [ 
          #"/run/wrappers" # Necessary for webzfs to run commands as sudo
          "${config.system.path}" # Put system packages in service environment
          smartmontools
        ];

      environment = {
        HOME = "/var/lib/webzfs";
        PYTHONPATH = webzfsDir;
        HOST = cfg.host;
        PORT = toString cfg.port;
        BIND_IP = cfg.host;
        CAPTION = "webzfs ${cfg.package.version or "git"}";
        SETTINGS_MODULE = "config.settings.base";
        SECRET_KEY = cfg.settings.SECRET_KEY or "changeme-in-production";
        WEBZFS_STATE_DIR = "/var/lib/webzfs";
      } // cfg.settings;

      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        Group = cfg.group;
        StateDirectory = "webzfs";
        StateDirectoryMode = "0750";
        WorkingDirectory = webzfsDir;
        Restart = "always";
        RestartSec = "5";
      };

      script = ''
        echo "Current PATH is: $PATH"
        exec ${cfg.package}/bin/webzfs
      '';
    };

    # Restricted NOPASSWD sudo rules mirroring upstream's documented list, on
    # the NixOS binary paths.  secure_path is pinned to /run/current-system/sw
    # /bin so the executed command matches these rules regardless of the
    # caller's PATH or rebuild churn (the symlink target changes, the path
    # does not).  Read-only access that is covered by group membership
    # (journalctl via `systemd-journal`) or works unprivileged (lsblk) is
    # intentionally not in sudo.
    security.sudo = {
      enable = true;
      extraRules = [
        {
          users = [ cfg.user ];
          commands = map (cmd: { command = cmd; options = [ "NOPASSWD" ]; }) [
            "${config.system.path}/bin/zpool"
            "${config.system.path}/bin/zfs"
            "${config.system.path}/bin/zdb -l *"
            "${config.system.path}/bin/lsof"
            "${config.system.path}/bin/lslocks"
            "${config.system.path}/bin/systemctl"
            "${config.system.path}/bin/crontab"
            "${config.system.path}/bin/tee /etc/systemd/system/webzfs-syncoid-job-*"
            "${config.system.path}/bin/rm -f /etc/systemd/system/webzfs-syncoid-job-*"
            "${config.system.path}/bin/tee /etc/systemd/system/webzfs-task-*"
            "${config.system.path}/bin/rm -f /etc/systemd/system/webzfs-task-*"
            "${config.system.path}/bin/cat"
            "${config.system.path}/bin/tee"
            "${config.system.path}/bin/mkdir"
            "${config.system.path}/bin/dmesg"
            "${pkgs.smartmontools}/bin/smartctl"
            "${pkgs.sanoid}/bin/sanoid"
            "${pkgs.sanoid}/bin/syncoid"
          ];
        }
      ];

    networking.firewall.allowedTCPPorts = lib.mkIf cfg.openFirewall [ cfg.port ];
  };
}