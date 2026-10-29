{
  lib,
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
  checkPhase = ''
    runHook preCheck
    PYTHONPATH="src:$PYTHONPATH" python -m unittest discover -s tests -v
    runHook postCheck
  '';

  pythonImportsCheck = [
    "brain.cli"
    "brain.tui"
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
