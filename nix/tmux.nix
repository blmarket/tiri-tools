{ lib, stdenv, tmux, fetchFromGitHub, jemalloc }:

# The tmux 3.8 webmon's integration tests pin; same revision as tiri's
# tmux-target, minus the source-tarball outputs only tiri needs.
tmux.overrideAttrs (old: {
  version = "3.8";
  src = fetchFromGitHub {
    owner = "tmux";
    repo = "tmux";
    rev = "7f2a35ad3321f9ba57a1062ca73b1f3ff26aca53";
    hash = "sha256-pv2wlr3coo+E0pfpb58giJJQn9PGgMFqtVA6nk+9JdE=";
  };
  patches = [ ];
  buildInputs = (old.buildInputs or [ ])
    ++ lib.optionals stdenv.hostPlatform.isDarwin [ jemalloc ];
  configureFlags =
    (lib.filter (flag: flag != "--enable-sixel") old.configureFlags)
    ++ [ "--disable-sixel" "--disable-debug" ]
    ++ lib.optionals stdenv.hostPlatform.isDarwin [ "--enable-jemalloc" ];
})
