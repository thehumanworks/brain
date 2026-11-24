{ brainPackages }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.programs.brain;
  # Environment= uses systemd quoting, not shell quoting. Escape specifiers,
  # but leave $ literal (unlike ExecStart=, Environment= does not expand it).
  quoteEnvironment = value: builtins.toJSON (lib.replaceStrings [ "%" ] [ "%%" ] value);

  # The agents `installHooks` sets up: those Home Manager configures, and those whose directory
  # exists at the switch, as bare `brain install` would choose them.
  home = config.home.homeDirectory;
  agents = [
    {
      flag = "--claude";
      module = "claude-code";
      dir = cfg.environment.CLAUDE_CONFIG_DIR or "${home}/.claude";
    }
    {
      flag = "--codex";
      module = "codex";
      dir = cfg.environment.CODEX_HOME or "${home}/.codex";
    }
    rec {
      flag = "--pi";
      module = "pi-coding-agent";
      dir = cfg.environment.PI_CODING_AGENT_DIR or "${home}/.pi/agent";
      # `brain install` replaces a link to the extension with a file, so one that Home Manager
      # links (the README's `home.file` line) is left to it: the next switch would fail on it.
      unless = "[[ $(realpath -m ${lib.escapeShellArg "${dir}/extensions/brain-brief.ts"}) == ${builtins.storeDir}/* ]]";
    }
  ];
  chooseAgent =
    agent:
    let
      used =
        if config.programs.${agent.module}.enable or false then
          "true"
        else
          "[[ -d ${lib.escapeShellArg agent.dir} ]]";
      unless = lib.optionalString (agent ? unless) " && ! ${agent.unless}";
    in
    "if ${used}${unless}; then brainHooks+=(${agent.flag}); fi";
  # The hooks name the profile's `brain`, which updates leave in place, rather than the store
  # path: Codex would ask to trust the hook again after every update.
  profileBin = lib.escapeShellArg "${config.home.profileDirectory}/bin";
  environment = lib.escapeShellArgs (
    lib.mapAttrsToList (name: value: "${name}=${value}") cfg.environment
  );
  index = lib.optionalString (
    cfg.environment ? BRAIN_DB
  ) "--db ${lib.escapeShellArg cfg.environment.BRAIN_DB}";
  installHooks = ''
    brainHooks=()
    ${lib.concatMapStringsSep "\n" chooseAgent agents}
    if (( ''${#brainHooks[@]} )); then
      run env PATH=${profileBin}:"$PATH" ${environment} ${lib.getExe cfg.package} ${index} \
        install --quiet "''${brainHooks[@]}" \
        || warnEcho "brain: a hook is not set up, as said above; \`brain install --check\` shows them all"
    fi
    unset brainHooks
  '';
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
      ]
    # Claude Code and Codex write their own settings files. Setting their hooks through Home
    # Manager either replaced those files or made them read-only, so the hooks are left to
    # `brain install`, which adds them to the files as they are.
    ++
      map
        (
          name:
          lib.mkRemovedOptionModule [ "programs" "brain" name ] ''
            Home Manager no longer writes the agents' settings for brain's hooks. Set
            `programs.brain.installHooks = true;` instead: at every switch it runs
            `brain install`, which adds the hook to each agent's own settings and leaves the rest
            of them alone. See "Agent hooks with Home Manager" in brain's README.
          ''
        )
        [
          "enableClaudeIntegration"
          "enableCodexIntegration"
          "enablePiIntegration"
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
        Nix store and must not contain secrets. `installHooks` uses them too.
      '';
    };

    installHooks = lib.mkOption {
      type = lib.types.bool;
      default = false;
      example = true;
      description = ''
        Whether to run `brain install` at every switch for Claude Code, Codex and Pi: each one
        Home Manager configures, and each whose directory exists. It adds brain's SessionStart
        hook to the agent's own settings, or Pi's extension, and changes nothing else in them;
        it prints only what it changed. The hooks name the profile's `brain`, so they stay as
        they are across updates, and so does Codex's trust in its hook.

        A settings file Home Manager links from the store cannot be changed: the switch carries
        on with a warning, and the hook is declared in Nix instead (see brain's README). Turning
        this off leaves the hooks in place; `brain uninstall --claude --codex --pi` removes them.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
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

    # After Home Manager has linked its files, put `brain` in the profile and merged its own
    # declarations into the agents' mutable settings, which would otherwise replace the
    # SessionStart list that holds brain's hook. Entries named here that are not there are left
    # out of the order.
    home.activation.brainHooks = lib.mkIf cfg.installHooks (
      lib.hm.dag.entryAfter [
        "writeBoundary"
        "linkGeneration"
        "installPackages"
        "claudeCodeSettings"
        "codexMutableSettings"
      ] installHooks
    );

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
