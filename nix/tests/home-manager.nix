{
  pkgs,
  brain,
  home-manager,
  module,
}:
let
  inherit (pkgs) lib;
  evaluate =
    extra:
    (home-manager.lib.homeManagerConfiguration {
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
    }).config;

  disabled = evaluate { };
  enabled = evaluate { programs.brain.enable = true; };
  # The module's name before it set the agents up as well.
  renamed = evaluate {
    services.brain = {
      enable = true;
      pollInterval = 30;
    };
  };

  exe = lib.getExe brain;
  hook = command: {
    matcher = "^(startup|clear)$";
    hooks = [
      {
        type = "command";
        inherit command;
        timeout = 5;
      }
    ];
  };
  integrations = {
    enable = true;
    enableClaudeIntegration = true;
    enableCodexIntegration = true;
    enablePiIntegration = true;
  };
  # Agents that Home Manager does not configure: their settings are files they write.
  unmanaged = evaluate { programs.brain = integrations; };
  # Agents that Home Manager configures, with hooks of the user's own.
  ownHook = {
    hooks = [
      {
        type = "command";
        command = "true";
      }
    ];
  };
  managed = evaluate {
    programs.brain = integrations;
    programs.claude-code = {
      enable = true;
      package = null;
      settings.hooks.SessionStart = [ ownHook ];
    };
    programs.codex = {
      enable = true;
      package = null;
      hooks.SessionStart = [ ownHook ];
    };
    programs.pi-coding-agent = {
      enable = true;
      package = null;
      configDir = "/home/brain-test/.config/pi";
    };
  };
  # Only one of the two agents under Home Manager, and the index and Codex moved.
  mixed = evaluate {
    programs.brain = integrations // {
      environment = {
        BRAIN_DB = "/data/my index.db";
        CODEX_HOME = "/home/brain-test/cx";
        PI_CODING_AGENT_DIR = "/home/brain-test/p";
      };
    };
    programs.claude-code = {
      enable = true;
      package = null;
    };
  };
  activation = config: config.home.activation.brainIntegrations.data;
  # Store paths are part of what is compared, which a pattern may not carry as context.
  has =
    infix: text:
    lib.hasInfix (builtins.unsafeDiscardStringContext infix) (builtins.unsafeDiscardStringContext text);
  # Home Manager's files by where they land, relative to the home directory.
  fileAt =
    config: target:
    (lib.findFirst (file: file.target == target) (throw "no file at ${target}") (
      lib.attrValues config.home.file
    )).source;
  linked =
    config:
    lib.filter (lib.hasSuffix "brain-brief.ts") (
      lib.mapAttrsToList (_: file: file.target) config.home.file
    );
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
    testNoIntegrationUnlessAsked = {
      expr = {
        activation = enabled.home.activation ? brainIntegrations;
        files = linked enabled;
      };
      expected = {
        activation = false;
        files = [ ];
      };
    };
    testUnmanagedAgentsAreSetUpAtActivation = {
      expr = has "PATH=\"${brain}/bin:$PATH\" ${exe} install --quiet --claude --codex " (
        activation unmanaged
      );
      expected = true;
    };
    testActivationGoesOnWhenAHookCannotBeAdded = {
      expr = has "|| warnEcho " (activation unmanaged);
      expected = true;
    };
    testPiExtensionIsLinked = {
      expr = {
        files = linked unmanaged;
        source = fileAt unmanaged ".pi/agent/extensions/brain-brief.ts";
      };
      expected = {
        files = [ ".pi/agent/extensions/brain-brief.ts" ];
        source = "${brain}/share/brain/pi/brain-brief.ts";
      };
    };
    testManagedAgentsGetTheHookInTheirOwnOptions = {
      expr = {
        activation = managed.home.activation ? brainIntegrations;
        claude = managed.programs.claude-code.settings.hooks.SessionStart;
        codex = managed.programs.codex.hooks.SessionStart;
        codexHooksOn = managed.programs.codex.settings.features.hooks;
        pi = linked managed;
      };
      expected = {
        activation = false;
        claude = [
          ownHook
          (hook "${exe} brief --hook")
        ];
        codex = [
          ownHook
          (hook "${exe} brief --hook")
        ];
        codexHooksOn = true;
        pi = [ ".config/pi/extensions/brain-brief.ts" ];
      };
    };
    testEnvironmentIsFollowed = {
      expr = {
        claude = mixed.programs.claude-code.settings.hooks.SessionStart;
        activation = has "run env 'BRAIN_DB=/data/my index.db' 'CODEX_HOME=/home/brain-test/cx' 'PI_CODING_AGENT_DIR=/home/brain-test/p' PATH=\"${brain}/bin:$PATH\" ${exe} --db '/data/my index.db' install --quiet --codex " (
          activation mixed
        );
        pi = linked mixed;
      };
      expected = {
        claude = [ (hook "${exe} --db '/data/my index.db' brief --hook") ];
        activation = true;
        pi = [ "p/extensions/brain-brief.ts" ];
      };
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

  # The files Home Manager writes for agents it configures, and the extension it links.
  cp ${fileAt managed ".claude/settings.json"} "$out/claude-settings.json"
  cp ${fileAt managed ".codex/hooks.json"} "$out/codex-hooks.json"
  cp ${fileAt managed ".codex/config.toml"} "$out/codex-config.toml"
  grep -F ${lib.escapeShellArg ''"command": "${exe} brief --hook"''} "$out/claude-settings.json"
  grep -F ${lib.escapeShellArg ''"command": "${exe} brief --hook"''} "$out/codex-hooks.json"
  grep -Fx 'hooks = true' "$out/codex-config.toml"
  grep -F ${lib.escapeShellArg ''|| "${exe}";''} ${fileAt managed ".config/pi/extensions/brain-brief.ts"}
''
