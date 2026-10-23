{
  description = "MCP server giving agents a private headless X11 desktop";

  inputs.nixpkgs.url = "nixpkgs";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      # Xvfb is the default server; tigervnc provides Xvnc, x11vnc backs `x11-mcp watch`.
      runtimeTools = pkgs: with pkgs; [ xvfb tigervnc openbox x11vnc dbus ];
      pythonDeps = p: with p; [ mcp xlib pillow ];
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.python3Packages.buildPythonApplication {
          pname = "x11-mcp";
          version = "0.1.0";
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = pythonDeps pkgs.python3Packages;
          makeWrapperArgs = [ "--prefix" "PATH" ":" (pkgs.lib.makeBinPath (runtimeTools pkgs)) ];
          meta.mainProgram = "x11-mcp";
        };
      });

      devShells = forAll (pkgs: {
        default = pkgs.mkShell {
          # xterm and iproute2 (ss) are only used by the live test.
          packages = [ (pkgs.python3.withPackages (p: pythonDeps p ++ [ p.pytest ])) pkgs.xterm pkgs.iproute2 ]
            ++ runtimeTools pkgs;
        };
      });
    };
}
