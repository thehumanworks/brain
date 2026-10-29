{ brainPackages }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.services.brain;
  # Environment= uses systemd quoting, not shell quoting. Escape specifiers,
  # but leave $ literal (unlike ExecStart=, Environment= does not expand it).
  quoteEnvironment = value: builtins.toJSON (lib.replaceStrings [ "%" ] [ "%%" ] value);
in
{
  options.services.brain = {
    enable = lib.mkEnableOption "continuous indexing of local agent conversation history";

    package = lib.mkOption {
      type = lib.types.package;
      default = brainPackages.${pkgs.stdenv.hostPlatform.system}.default;
      defaultText = lib.literalExpression "inputs.brain.packages.${pkgs.stdenv.hostPlatform.system}.default";
      description = "The brain package to install and run.";
    };

    pollInterval = lib.mkOption {
      type = lib.types.ints.positive;
      default = 5;
      example = 30;
      description = ''
        Seconds between transcript scans and incremental index updates.
        A scan runs immediately on startup. Scans never overlap; if a scan
        takes longer than this interval, the next starts when it finishes.
        This does not delete or rebuild the index from scratch.
      '';
    };

    environment = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      default = { };
      example = {
        CODEX_HOME = "/home/alice/custom-codex";
        BRAIN_DB = "/home/alice/.local/share/brain/index.db";
      };
      description = ''
        Environment variables for the watcher, such as CLAUDE_CONFIG_DIR,
        CODEX_HOME, PI_CODING_AGENT_DIR, or BRAIN_DB. Use absolute paths;
        shell variables and tilde are not expanded. Values are stored in the
        Nix store and must not contain secrets.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = pkgs.stdenv.hostPlatform.isLinux;
        message = "services.brain requires Linux with a systemd user manager.";
      }
      {
        assertion = lib.all (name: builtins.match "[A-Za-z_][A-Za-z0-9_]*" name != null) (
          builtins.attrNames cfg.environment
        );
        message = "services.brain.environment must use valid environment variable names.";
      }
    ];

    home.packages = [ cfg.package ];

    # Respect an explicit user preference not to start services at activation.
    systemd.user.startServices = lib.mkDefault "sd-switch";
    systemd.user.services.brain = {
      Unit.Description = "Index Claude Code, Codex and Pi conversation history";
      Service = {
        ExecStart = "${lib.getExe cfg.package} watch --interval ${toString cfg.pollInterval}";
        Environment = lib.mapAttrsToList (name: value: quoteEnvironment "${name}=${value}") cfg.environment;
        Restart = "always";
        RestartSec = 30;
        Nice = 10;
        IOSchedulingClass = "idle";
      };
      Install.WantedBy = [ "default.target" ];
    };
  };
}
