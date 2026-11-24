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
''
