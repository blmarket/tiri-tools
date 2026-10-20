{
  description = "tiri tools: agentmon and webmon";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs, ... }:
    let
      supportedSystems = [
        "aarch64-darwin"
        "x86_64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        rec {
          agentmon = pkgs.callPackage ./agentmon-tui/package.nix { };
          webmon = pkgs.callPackage ./webmon/package.nix { };
          default = pkgs.symlinkJoin {
            name = "tiri-tools";
            paths = [ agentmon webmon ];
          };
        });
    };
}
