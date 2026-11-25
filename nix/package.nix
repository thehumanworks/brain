{
  lib,
  git,
  python314Packages,
}:
let
  project = (builtins.fromTOML (builtins.readFile ../pyproject.toml)).project;
in
python314Packages.buildPythonApplication {
  pname = project.name;
  inherit (project) version;
  pyproject = true;

  src = lib.fileset.toSource {
    root = ../.;
    fileset = lib.fileset.unions [
      ../pyproject.toml
      ../README.md
      ../src
      ../tests
    ];
  };

  build-system = [ python314Packages.hatchling ];
  dependencies = [ python314Packages.prompt-toolkit ];

  doCheck = true;
  nativeCheckInputs = [ git ];
  checkPhase = ''
    runHook preCheck
    PYTHONPATH="src:$PYTHONPATH" python -m unittest discover -s tests -v
    runHook postCheck
  '';

  # The Pi extension as `brain install --pi` writes it, for a Home Manager `home.file` to link.
  postInstall = ''
    install -Dm444 src/brain/integrations/pi/brain-brief.ts "$out/share/brain/pi/brain-brief.ts"
    substituteInPlace "$out/share/brain/pi/brain-brief.ts" \
      --replace-fail '|| "brain";' "|| \"$out/bin/brain\";"
  '';

  pythonImportsCheck = [
    "brain"
    "brain.cli"
    "brain.cli.tui"
    "compression.zstd"
    "sqlite3"
  ];

  meta = {
    inherit (project) description;
    homepage = "https://github.com/thehumanworks/brain";
    mainProgram = "brain";
    platforms = lib.platforms.linux;
  };
}
