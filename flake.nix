{
  description = "Local full-text search over Claude Code, Codex and Pi conversation history";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    # Used to check the exported module against the real Home Manager options.
    home-manager = {
      url = "github:nix-community/home-manager";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      home-manager,
    }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
      pkgsFor = system: nixpkgs.legacyPackages.${system};
    in
    {
      homeManagerModules = {
        brain = import ./nix/home-manager.nix { brainPackages = self.packages; };
        default = self.homeManagerModules.brain;
      };

      packages = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          brain = pkgs.callPackage ./nix/package.nix { };
        in
        {
          inherit brain;
          default = brain;
        }
      );

      apps = forAllSystems (system: {
        default = self.apps.${system}.brain;
        brain = {
          type = "app";
          program = nixpkgs.lib.getExe self.packages.${system}.brain;
          meta.description = self.packages.${system}.brain.meta.description;
        };
      });

      checks = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
          brain = self.packages.${system}.brain;
        in
        {
          package = brain;
          home-manager = import ./nix/tests/home-manager.nix {
            inherit pkgs brain home-manager;
            module = self.homeManagerModules.default;
          };
          cli = pkgs.runCommand "brain-cli-check" { nativeBuildInputs = [ brain ]; } ''
            export HOME="$TMPDIR/home"
            mkdir -p "$HOME"
            brain --help
            brain index
            brain stats
            test "$(brain list --json)" = "[]"
            test "$(brain search smoke-test --json)" = "[]"
            touch "$out"
          '';
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = pkgsFor system;
        in
        {
          default = pkgs.mkShell {
            inputsFrom = [ self.packages.${system}.brain ];
            packages = [
              (pkgs.python314.withPackages (ps: [ ps.prompt-toolkit ]))
              pkgs.uv
              pkgs.nixfmt
            ];

            # Use the checkout directly, without creating a venv or downloading Python.
            shellHook = ''
              export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"
              export UV_PYTHON="${pkgs.python314}/bin/python3"
              export UV_PYTHON_DOWNLOADS=never
            '';
          };
        }
      );

      formatter = forAllSystems (system: (pkgsFor system).nixfmt-tree);
    };
}
