#!/usr/bin/env bash
# Set this checkout up on Linux -- a cloud session, a container, a laptop --
# where the point is the console and not the bench.
#
# The measurement half needs no hardware to develop against: `--sim` builds the
# rig from `bace.drivers.simulated` and never imports pyvisa, so the `rig` extra
# is deliberately NOT installed here. What you get is the whole API, the whole
# event stream and the whole journal, with a cryostat that settles in the time a
# click takes.
#
# Two things about this service that surprise people on a sandbox:
#
#   * `--host` is refused unless it is a loopback address. The service has no
#     auth and owns every instrument, so binding it where a network can reach it
#     is not a configuration choice. If your sandbox exposes ports by proxying
#     localhost you need nothing; if it wants the process on 0.0.0.0, put the
#     proxy in front rather than editing the check.
#
#   * `rig.toml` and `run.toml` are found by name in the working directory. Run
#     from the repo root -- from anywhere else the service falls back to the
#     built-in defaults, which is a recipe nobody chose. This script cd's there.
#
# Override the interpreter with PYTHON=..., skip the self-check with NO_TEST=1.

set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}

if ! command -v "$PY" >/dev/null 2>&1; then
    echo "no interpreter: '$PY' is not on PATH. Set PYTHON=... to choose one." >&2
    exit 1
fi

# 3.11 is the floor because config loading uses tomllib.
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "$PY is $("$PY" -c 'import platform; print(platform.python_version())'), and bace needs 3.11+ (tomllib)." >&2
    exit 1
fi

echo "== installing into $("$PY" -c 'import sys; print(sys.prefix)')"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -e '.[service,dev]'

echo "== what came in"
"$PY" - <<'EOF'
import importlib.metadata as md
for name in ("numpy", "scipy", "h5py", "fastapi", "uvicorn", "websockets", "pytest"):
    try:
        print(f"   {name:<12} {md.version(name)}")
    except md.PackageNotFoundError:
        raise SystemExit(f"   {name:<12} MISSING -- the install did not take")
import bace.service            # noqa: F401  -- imports fastapi, so it fails loudly here
print("   bace.service  imports")
EOF

if [ "${NO_TEST:-}" = "1" ]; then
    echo "== self-check skipped (NO_TEST=1)"
else
    echo "== self-check"
    # Everything runs on the simulator; nothing here touches an instrument.
    # One test fails only on the machine this port was written on (a stray
    # 64-bit delib64.dll in System32 makes the DIO backend findable when the
    # test needs it absent). On Linux there is no DELIB and it should pass.
    "$PY" -m pytest -q
fi

cat <<'EOF'

== ready

  serve the API with a simulated rig, every settle a no-op:

      python -m bace.service --sim --fast --port 8900

  and once ui/ exists, serve it alongside at /ui (/ redirects there):

      python -m bace.service --sim --fast --port 8900 --ui ui/

  GET / lists every route, /docs is the interactive page, and
  bace/service/README.md walks the whole operator flow by hand.
  docs/README.md indexes the documentation.
EOF
