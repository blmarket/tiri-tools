{ lib, python3Packages, tmux, openssh, callPackage }:

python3Packages.buildPythonApplication {
  pname = "hmux-webmon";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = with python3Packages; [
    hatchling
  ];
  dependencies = [
    python3Packages.websockets
    python3Packages.cryptography
    (callPackage ../agentmon-tui/package.nix { })
  ];
  nativeCheckInputs = [
    tmux
    openssh
    python3Packages.pytestCheckHook
  ];
  SUT = "stock";
  pythonImportsCheck = [ "webmon" ];
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [ tmux openssh ])
  ];

  meta.mainProgram = "webmon";
}
