{
  description = "WebZFS - Web-based ZFS management interface and NixOS module";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;

      overlay = final: prev: {
        webzfs = final.callPackage ./nix/package.nix { src = self; };
      };
    in
    {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; overlays = [ overlay ]; };
        in
        {
          webzfs = pkgs.webzfs;
          default = pkgs.webzfs;
        }
      );

      nixosModules.webzfs = import ./nix/module.nix;

      overlays.default = overlay;

      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in
        {
          default = import ./nix/dev-shell.nix { inherit pkgs; };
        }
      );
    };
}
