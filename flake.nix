{
  description = "WebZFS - Web-based ZFS management interface and NixOS module";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = 
    { 
      self, 
      nixpkgs 
    }:
    {

      nixosModules = rec {
        webzfs = import ./ports/nix/module.nix;
        default = { ... }: {
          imports = [ webzfs ];
        };
      };

    };
}
