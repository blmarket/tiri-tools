{ lib, python3Packages, git }:

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
  # Suffixed, not prefixed: a fallback for a bare environment. The tmux client
  # is the caller's, taken from `PATH`.
  makeWrapperArgs = [
    "--suffix"
    "PATH"
    ":"
    (lib.makeBinPath [ git ])
  ];

  meta.mainProgram = "agentmon";
}
