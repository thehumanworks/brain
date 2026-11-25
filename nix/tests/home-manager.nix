{
  pkgs,
  brain,
  home-manager,
  module,
}:
let
  inherit (pkgs) lib;
  configuration =
    extra:
    home-manager.lib.homeManagerConfiguration {
      inherit pkgs;
      modules = [
        module
        {
          home.username = "brain-test";
          home.homeDirectory = "/home/brain-test";
          home.stateVersion = "26.05";
        }
        extra
      ];
    };
  evaluate = extra: (configuration extra).config;

  disabled = evaluate { };
  enabled = evaluate { programs.brain.enable = true; };
  # The module's name before 0.2.0.
  renamed = evaluate {
    services.brain = {
      enable = true;
      pollInterval = 30;
    };
  };

  exe = lib.getExe brain;
  # Agents that Home Manager configures, with no settings of the user's own: brain must not
  # make Home Manager take over the files these agents write themselves.
  agents = evaluate {
    programs.brain.enable = true;
    programs.claude-code = {
      enable = true;
      package = null;
    };
    programs.codex = {
      enable = true;
      package = null;
    };
    programs.pi-coding-agent = {
      enable = true;
      package = null;
    };
  };
  targets = config: lib.mapAttrsToList (_: file: file.target) config.home.file;
  removedIntegrations = [
    "enableClaudeIntegration"
    "enableCodexIntegration"
    "enablePiIntegration"
  ];
  fails = extra: !(builtins.tryEval (evaluate extra).programs.brain.enable).success;

  withHooks =
    extra:
    configuration {
      imports = [
        {
          programs.brain = {
            enable = true;
            installHooks = true;
          };
        }
        extra
      ];
    };
  # No agent configured by Home Manager: each is chosen by its directory at the switch.
  hooksAlone = withHooks { };
  # Each agent configured, Claude Code and Codex with settings Home Manager merges into theirs.
  hooksWithAgents = withHooks {
    programs.claude-code = {
      enable = true;
      package = null;
      mutableSettings = true;
      settings.theme = "dark";
    };
    programs.codex = {
      enable = true;
      package = null;
      mutableSettings = true;
      settings.model = "gpt-5";
    };
    programs.pi-coding-agent = {
      enable = true;
      package = null;
    };
  };
  hooksWithDb = withHooks { programs.brain.environment.BRAIN_DB = "/home/brain-test/b.db"; };
  hookStep = configuration: configuration.config.home.activation.brainHooks;
  profileBin = "${hooksAlone.config.home.profileDirectory}/bin";
  # The step's text without the store paths' context, which `hasInfix` cannot match against.
  hookText = configuration: builtins.unsafeDiscardStringContext (hookStep configuration).data;
  replacement = pkgs.writeShellScriptBin "brain" "exit 0";
  customized = evaluate {
    programs.brain = {
      enable = true;
      package = replacement;
      pollInterval = 30;
      environment.CODEX_HOME = "/home/brain-test/a b/100%/$literal/\"quoted\"";
    };
    systemd.user.startServices = false;
  };
  invalidEnvironment = evaluate {
    programs.brain = {
      enable = true;
      environment."INVALID=NAME" = "value";
    };
  };
  acceptsInterval =
    interval:
    (builtins.tryEval
      (evaluate {
        programs.brain = {
          enable = true;
          pollInterval = interval;
        };
      }).programs.brain.pollInterval
    ).success;

  tests = lib.runTests {
    testDisabled = {
      expr = disabled.systemd.user.services ? brain || builtins.elem brain disabled.home.packages;
      expected = false;
    };
    testDefaultPackage = {
      expr = builtins.elem brain enabled.home.packages;
      expected = true;
    };
    testDefaultCommand = {
      expr = enabled.systemd.user.services.brain.Service.ExecStart;
      expected = [ "${lib.getExe brain} watch --interval 5" ];
    };
    testStartup = {
      expr = enabled.systemd.user.services.brain.Install.WantedBy;
      expected = [ "default.target" ];
    };
    testDefaultActivation = {
      expr = enabled.systemd.user.startServices;
      expected = true;
    };
    testOverrides = {
      expr = {
        installed = builtins.elem replacement customized.home.packages;
        command = customized.systemd.user.services.brain.Service.ExecStart;
        autoStart = customized.systemd.user.startServices;
      };
      expected = {
        installed = true;
        command = [ "${lib.getExe replacement} watch --interval 30" ];
        autoStart = false;
      };
    };
    testEnvironmentQuoting = {
      expr = customized.systemd.user.services.brain.Service.Environment;
      expected = [ ''"CODEX_HOME=/home/brain-test/a b/100%%/$literal/\"quoted\""'' ];
    };
    testValidAssertions = {
      expr = lib.all (a: a.assertion) enabled.assertions;
      expected = true;
    };
    testInvalidEnvironment = {
      expr = (builtins.tryEval invalidEnvironment.programs.brain.enable).success;
      expected = false;
    };
    testOldNameStillWorks = {
      expr = renamed.systemd.user.services.brain.Service.ExecStart;
      expected = [ "${exe} watch --interval 30" ];
    };
    testAgentsSettingsAreLeftAlone = {
      expr = {
        files = lib.filter (
          target:
          lib.elem target [
            ".claude/settings.json"
            ".codex/config.toml"
            ".codex/hooks.json"
          ]
          || lib.hasSuffix "brain-brief.ts" target
        ) (targets agents);
        activation = lib.filter (lib.hasPrefix "brain") (lib.attrNames agents.home.activation);
        claude = agents.programs.claude-code.settings;
        codex = agents.programs.codex.hooks;
      };
      expected = {
        files = [ ];
        activation = [ ];
        claude = { };
        codex = { };
      };
    };
    # The removed options are still declared, so that setting one fails with what to do
    # instead rather than as an unknown option.
    testRemovedIntegrationsAreDeclared = {
      expr = map (name: (configuration { }).options.programs.brain ? ${name}) removedIntegrations;
      expected = map (_: true) removedIntegrations;
    };
    testRemovedIntegrationsFail = {
      expr = map (
        name:
        fails {
          programs.brain = {
            enable = true;
            ${name} = true;
          };
        }
      ) removedIntegrations;
      expected = map (_: true) removedIntegrations;
    };
    testInstallHooksRunsAfterHomeManagersOwnWrites = {
      expr = (hookStep hooksAlone).after;
      expected = [
        "writeBoundary"
        "linkGeneration"
        "installPackages"
        "claudeCodeSettings"
        "codexMutableSettings"
      ];
    };
    testInstallHooksNamesTheProfile = {
      expr = {
        inProfile = lib.hasInfix "PATH=${lib.escapeShellArg profileBin}:" (hookText hooksAlone);
        inStore = lib.hasPrefix builtins.storeDir profileBin;
      };
      expected = {
        inProfile = true;
        inStore = false;
      };
    };
    testInstallHooksTakesConfiguredAgents = {
      expr = map (line: lib.hasInfix line (hookText hooksWithAgents)) [
        "if true; then brainHooks+=(--claude); fi"
        "if true; then brainHooks+=(--codex); fi"
        "if true && ! [[ $(realpath -m ${lib.escapeShellArg "/home/brain-test/.pi/agent/extensions/brain-brief.ts"}) == ${builtins.storeDir}/* ]]; then brainHooks+=(--pi); fi"
      ];
      expected = [
        true
        true
        true
      ];
    };
    testInstallHooksUsesTheIndex = {
      expr = lib.hasInfix (lib.concatStringsSep " " [
        (lib.escapeShellArg "BRAIN_DB=/home/brain-test/b.db")
        (builtins.unsafeDiscardStringContext exe)
        "--db"
        (lib.escapeShellArg "/home/brain-test/b.db")
      ]) (hookText hooksWithDb);
      expected = true;
    };
    testInstallHooksLeavesAgentsFilesAlone = {
      expr = lib.filter (
        target:
        lib.elem target [
          ".claude/settings.json"
          ".codex/config.toml"
          ".codex/hooks.json"
        ]
        || lib.hasSuffix "brain-brief.ts" target
      ) (targets hooksWithAgents.config);
      expected = [ ];
    };
    testIntervalValidation = {
      expr = map acceptsInterval [
        0
        (-1)
        "5"
        1.5
        1
        30
      ];
      expected = [
        false
        false
        false
        false
        true
        true
      ];
    };
  };
in
assert lib.assertMsg (tests == [ ]) "Home Manager module tests failed: ${builtins.toJSON tests}";
pkgs.runCommand "brain-home-manager-check" { } ''
  # Build the actual unit files generated by Home Manager, not just option values.
  mkdir -p "$out"
  cp ${enabled.xdg.configFile."systemd/user/brain.service".source} "$out/default.service"
  cp ${customized.xdg.configFile."systemd/user/brain.service".source} "$out/custom.service"
  grep -Fx ${lib.escapeShellArg "ExecStart=${lib.getExe brain} watch --interval 5"} "$out/default.service"
  grep -Fx ${lib.escapeShellArg "ExecStart=${lib.getExe replacement} watch --interval 30"} "$out/custom.service"
  grep -Fx 'WantedBy=default.target' "$out/default.service"

  # brain's step comes after Home Manager's merges into the agents' settings, in the real
  # activation script, and one without those steps still builds.
  activate=${hooksWithAgents.activationPackage}/activate
  test -e ${hooksAlone.activationPackage}/activate
  line() { grep -nF -m1 "$1" "$activate" | cut -d: -f1; }
  test "$(line '"claudeCodeSettings"')" -lt "$(line 'brainHooks=()')"
  test "$(line '"codexMutableSettings"')" -lt "$(line 'brainHooks=()')"

  # The step itself, as the activation script runs it, in a home of its own.
  export HOME=$PWD/home
  profile=$(sed "s|^/home/brain-test|$HOME|" <<< ${lib.escapeShellArg profileBin})
  mkdir -p "$profile" "$HOME/.claude" "$HOME/.codex" "$HOME/.pi/agent/extensions"
  ln -s ${exe} "$profile/brain"
  # Pi's extension as the README's `home.file` line links it.
  ln -s ${brain}/share/brain/pi/brain-brief.ts "$HOME/.pi/agent/extensions/brain-brief.ts"
  sed "s|/home/brain-test|$HOME|g" ${pkgs.writeText "brain-hooks" (hookStep hooksAlone).data} > hooks.sh
  run() { "$@"; }
  warnEcho() { echo "warning: $*"; }
  set -euo pipefail

  source ./hooks.sh > first.txt 2>&1
  cat first.txt
  grep -Fx 'claude   added the SessionStart hook in ~/.claude/settings.json' first.txt
  grep -Fx 'codex    added the SessionStart hook in ~/.codex/hooks.json' first.txt
  if grep '^pi ' first.txt; then exit 1; fi
  test -L "$HOME/.pi/agent/extensions/brain-brief.ts"
  grep -F "\"$profile/brain brief --hook\"" "$HOME/.claude/settings.json"
  grep -F "\"$profile/brain brief --hook\"" "$HOME/.codex/hooks.json"

  source ./hooks.sh > second.txt 2>&1
  test ! -s second.txt

  # A settings file another tool keeps read-only: a warning, and the switch carries on.
  mkdir kept
  echo '{}' > kept/settings.json
  chmod a-w kept kept/settings.json
  ln -sf "$PWD/kept/settings.json" "$HOME/.claude/settings.json"
  source ./hooks.sh > third.txt 2>&1
  cat third.txt
  grep -F 'claude   failed: cannot write ~/.claude/settings.json' third.txt
  grep -F 'warning: brain: a hook is not set up' third.txt
  chmod u+w kept
''
