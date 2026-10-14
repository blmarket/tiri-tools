{ lib, python3Packages, git, tmux }:

# Package the `agentmon` dashboard and its companion `looper` together.
python3Packages.buildPythonApplication {
  pname = "tiri-agentmon";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = with python3Packages; [
    hatchling
  ];
  dependencies = with python3Packages; [
    textual
  ];
  nativeCheckInputs = [
    git
    python3Packages.pytestCheckHook
  ];
  pythonImportsCheck = [ "agentmon" ];
  # Suffixed, not prefixed: these are a fallback for a bare environment. A
  # caller that pins its own `tmux` — a checkout testing one version against
  # another does — must keep it ahead of nixpkgs'.
  makeWrapperArgs = [
    "--suffix"
    "PATH"
    ":"
    (lib.makeBinPath [
      git
      tmux
    ])
  ];

  meta.mainProgram = "agentmon";
}
