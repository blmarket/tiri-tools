{ lib, python3Packages, openssh, callPackage }:

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
    openssh
    python3Packages.pytestCheckHook
  ];
  pythonImportsCheck = [ "webmon" ];
  makeWrapperArgs = [
    "--prefix"
    "PATH"
    ":"
    (lib.makeBinPath [ openssh ])
  ];

  meta.mainProgram = "webmon";
}
