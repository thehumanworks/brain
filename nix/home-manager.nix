{ brainPackages }:
{
  config,
  lib,
  options,
  pkgs,
  ...
}:
let
  cfg = config.programs.brain;
  # Environment= uses systemd quoting, not shell quoting. Escape specifiers,
  # but leave $ literal (unlike ExecStart=, Environment= does not expand it).
  quoteEnvironment = value: builtins.toJSON (lib.replaceStrings [ "%" ] [ "%%" ] value);

  # The watcher finds an index that `environment` moves through its unit. A hook runs in the
  # agent's environment instead, so it has to be told.
  brain = [
    (lib.getExe cfg.package)
  ]
  ++ lib.optionals (cfg.environment ? BRAIN_DB) [
    "--db"
    cfg.environment.BRAIN_DB
  ];
  hook = {
    matcher = "^(startup|clear)$";
    hooks = [
      {
        type = "command";
        command = "${lib.escapeShellArgs brain} brief --hook";
        timeout = 5;
      }
    ];
  };

  # An agent that Home Manager configures gets the hook through that module's own options.
  # Otherwise the agent's settings are a file the agent itself writes, and `brain install`
  # adds the hook to it when the configuration is activated.
  managed = name: options.programs ? ${name} && config.programs.${name}.enable;
  claudeManaged = managed "claude-code";
  codexManaged = managed "codex";
  installed =
    lib.optional (cfg.enableClaudeIntegration && !claudeManaged) "--claude"
    ++ lib.optional (cfg.enableCodexIntegration && !codexManaged) "--codex";

  piDirectory =
    cfg.environment.PI_CODING_AGENT_DIR or (
      if managed "pi-coding-agent" then
        config.programs.pi-coding-agent.configDir
      else
        "${config.home.homeDirectory}/.pi/agent"
    );

  integration =
    agent: how:
    lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Whether to start each ${agent} session someone starts with
        {command}`brain brief`: the latest earlier sessions in its project.
        ${how}
      '';
    };
in
{
  imports =
    map
      (
        name:
        lib.mkRenamedOptionModule
          [
            "services"
            "brain"
            name
          ]
          [
            "programs"
            "brain"
            name
          ]
      )
      [
        "enable"
        "package"
        "pollInterval"
        "environment"
      ];

  options.programs.brain = {
    enable = lib.mkEnableOption "brain, with continuous indexing of local agent conversation history";

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
        Nix store and must not contain secrets. The integrations follow them
        too: they find each agent's settings and the index where these say.
      '';
    };

    enableClaudeIntegration = integration "Claude Code" ''
      With {option}`programs.claude-code.enable`, the SessionStart hook is added to
      {option}`programs.claude-code.settings`. Otherwise {command}`brain install --claude`
      adds it to {file}`~/.claude/settings.json` at activation, leaving the rest of that
      file as it is.
    '';

    enableCodexIntegration = integration "Codex" ''
      With {option}`programs.codex.enable`, the SessionStart hook is added to
      {option}`programs.codex.hooks` and hooks are turned on in
      {option}`programs.codex.settings`. Otherwise {command}`brain install --codex` does
      both in {file}`~/.codex` at activation. Codex runs a new hook only once you trust
      it: review it with {command}`/hooks` in Codex.
    '';

    enablePiIntegration = integration "Pi" ''
      Links brain's extension into {file}`~/.pi/agent/extensions`, or into the
      directory {option}`programs.pi-coding-agent.configDir` names.
    '';
  };

  config = lib.mkIf cfg.enable (
    lib.mkMerge [
      {
        assertions = [
          {
            assertion = pkgs.stdenv.hostPlatform.isLinux;
            message = "programs.brain requires Linux with a systemd user manager.";
          }
          {
            assertion = lib.all (name: builtins.match "[A-Za-z_][A-Za-z0-9_]*" name != null) (
              builtins.attrNames cfg.environment
            );
            message = "programs.brain.environment must use valid environment variable names.";
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
      }

      (lib.mkIf cfg.enablePiIntegration {
        # `force`: a copy that `brain install --pi` left there earlier is brain's own file.
        home.file."${piDirectory}/extensions/brain-brief.ts" = {
          source = "${cfg.package}/share/brain/pi/brain-brief.ts";
          force = true;
        };
      })

      (lib.mkIf (installed != [ ]) {
        # A hook that cannot be added must not stop the rest of the activation.
        home.activation.brainIntegrations = lib.hm.dag.entryAfter [ "writeBoundary" ] ''
          run env ${
            lib.escapeShellArgs (lib.mapAttrsToList (name: value: "${name}=${value}") cfg.environment)
          } PATH="${cfg.package}/bin:$PATH" ${lib.escapeShellArgs brain} install --quiet ${toString installed} \
            || warnEcho "brain: a session-start hook is not set up; see the lines above"
        '';
      })

      (lib.optionalAttrs (options.programs ? claude-code) {
        programs.claude-code.settings = lib.mkIf (cfg.enableClaudeIntegration && claudeManaged) {
          hooks.SessionStart = [ hook ];
        };
      })

      (lib.optionalAttrs (options.programs ? codex) {
        programs.codex = lib.mkIf (cfg.enableCodexIntegration && codexManaged) {
          hooks.SessionStart = [ hook ];
          settings.features.hooks = true;
        };
      })
    ]
  );
}
