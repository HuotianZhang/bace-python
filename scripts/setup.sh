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
# What it verifies, in order: the interpreter, the install, the Python suite,
# and then the one command it tells you to run -- booted on a real socket with
# the real ui/ mounted, because no test serves that (they mount a stub in a
# temporary folder). What it cannot verify is the console's own suite: ui/ is
# plain ES modules run by Node, a desk tool the lab PC does not have, so those
# skip when Node is absent. The script says which case you are in rather than
# reporting "ready" over a silent hole.
#
# Knobs: PYTHON=... chooses the interpreter, EXTRAS=rig adds an extra on top of
# service,dev, NO_TEST=1 skips the suite, NO_SMOKE=1 skips the start-up check,
# BACE_VENV=... names the venv and BACE_NO_VENV=1 does without one.

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

# A distro interpreter is not ours to install into. On Debian and Ubuntu -- which
# is what a cloud session or a container is, nine times out of ten -- pip cannot
# even upgrade itself there ("Cannot uninstall pip: RECORD file not found"), and
# on images carrying the PEP 668 marker it refuses the install outright. So
# unless we are already inside one, build a venv and use that. BACE_VENV=...
# names it elsewhere; BACE_NO_VENV=1 installs into "$PY" as it stands, which is
# what you want when the interpreter is already the container's own.
VENV=""
if [ "${BACE_NO_VENV:-}" != "1" ] \
   && ! "$PY" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)'; then
    VENV=${BACE_VENV:-.venv}
    echo "== $PY is a system interpreter; building $VENV rather than installing into it"
    "$PY" -m venv "$VENV"
    PY="$PWD/$VENV/bin/python"
fi

# `service,dev` is a console checkout: the API, the suite, and deliberately no
# VISA. EXTRAS=rig adds pyvisa on top, for a Linux box that does have the
# instruments; it adds to the pair rather than replacing it, so the check below
# stays true whatever is asked for.
EXTRAS="service,dev${EXTRAS:+,$EXTRAS}"

echo "== installing into $("$PY" -c 'import sys; print(sys.prefix)')  [.[$EXTRAS]]"
# A newer pip is a nicety. Never fail the setup over one.
"$PY" -m pip install --quiet --upgrade pip || echo "   (pip could not upgrade itself; carrying on)"
"$PY" -m pip install --quiet -e ".[$EXTRAS]"

echo "== what came in"
"$PY" - <<'EOF'
import importlib.metadata as md
for name in ("numpy", "scipy", "h5py", "fastapi", "uvicorn", "websockets", "pytest", "httpx"):
    try:
        print(f"   {name:<12} {md.version(name)}")
    except md.PackageNotFoundError:
        raise SystemExit(f"   {name:<12} MISSING -- the install did not take")
import bace.service            # noqa: F401  -- imports fastapi, so it fails loudly here
print("   bace.service  imports")
EOF

# ui/ has no build step, so pip installs none of it and the block above checks
# none of it: the console's 20 suites are Node's own runner and its one lint
# rule is eslint's. Both skip rather than fail when absent, which is right for
# the lab PC -- it has neither -- and a trap on a desk machine, where "ready"
# would then cover a console nothing had run. Name which case this is.
echo "== the console half"
# Counted with a glob rather than `ls | wc -l`: under `set -o pipefail` a glob
# that matches nothing takes the whole script down with a bare exit 2, which is
# the one way a setup script must never fail.
shopt -s nullglob
CONSOLE_SUITES=(ui/tests/*.test.mjs)
shopt -u nullglob
SUITES=${#CONSOLE_SUITES[@]}
if [ "$SUITES" -eq 0 ]; then
    echo "   ui/tests/    no suites found. Either this checkout is missing ui/,"
    echo "                or the console's tests have moved and this line is stale."
elif command -v node >/dev/null 2>&1; then
    echo "   node         $(node --version), so the $SUITES suites under ui/tests/ run below"
else
    echo "   node         NOT FOUND, so $SUITES suites under ui/tests/ will skip."
    echo "                Expected on the lab PC; on a desk machine the console is"
    echo "                unverified until you install Node."
fi
if command -v eslint >/dev/null 2>&1 || npx --no-install eslint --version >/dev/null 2>&1; then
    echo "   eslint       present, so the no-undef pass over ui/ runs below"
else
    echo "   eslint       NOT FOUND, so the no-undef pass over ui/ will skip."
    echo "                npm install -g eslint, or npx eslint once with a network."
fi

if [ "${NO_TEST:-}" = "1" ]; then
    echo "== self-check skipped (NO_TEST=1)"
else
    echo "== self-check"
    # Everything runs on the simulator; nothing here touches an instrument.
    # One test fails only on the machine this port was written on (a stray
    # 64-bit delib64.dll in System32 makes the DIO backend findable when the
    # test needs it absent). On Linux there is no DELIB and it should pass.
    # -rs names every skip. A skip is a check that did not happen, and the
    # only ones left on a clean checkout should be ones you can explain.
    "$PY" -m pytest -q -rs
fi

if [ "${NO_SMOKE:-}" = "1" ]; then
    echo "== start-up check skipped (NO_SMOKE=1)"
else
    echo "== start-up check"
    # The suite boots the CLI in a subprocess, but every test that mounts a
    # console mounts a stub index.html from a temporary folder -- nothing has
    # ever served the real ui/. So run the exact command the block below
    # prints, on a port the OS says is free, and check what a browser would
    # get: the bench, the redirect, and the console's own page. The session
    # goes to a temporary --out, so setting up leaves no journal behind.
    "$PY" - <<'EOF'
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]

out = tempfile.mkdtemp(prefix="bace-setup-")
proc = subprocess.Popen(
    [sys.executable, "-m", "bace.service", "--sim", "--fast",
     "--port", str(port), "--ui", "ui/", "--out", out],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def get(path, timeout=5.0):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout)

def die(why):
    proc.kill()
    raise SystemExit(f"   {why}\n{proc.stdout.read() or '(the service said nothing)'}")

try:
    bench = None
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            die("the service exited before it answered:")
        try:
            with get("/bench", timeout=2.0) as r:
                bench = json.loads(r.read().decode())
            break
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.2)
    if bench is None:
        die(f"nothing answered on 127.0.0.1:{port} within 60 s:")

    session, chain = bench["session"], bench["chain"]
    print(f"   /bench       {session['mode']}"
          f"{' fast' if session.get('fast') else ''}, "
          f"{len(bench['instruments'])} instruments, "
          f"chain {chain['ok']}/{chain['total']}")

    try:
        with get("/") as r:                 # urllib follows the redirect for us
            landed = r.geturl()
        with get("/ui/") as r:
            page = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:   # a 404 here is a mount that is wrong
        die(f"{exc.url} answered {exc.code} {exc.reason}:")
    if not landed.endswith("/ui/"):
        die(f"/ should land on the console and landed on {landed}:")
    if "<title>BACE console</title>" not in page:
        die("/ui/ answered, but with something that is not the console's index.html:")
    print(f"   / -> /ui/    the real ui/, {len(page)} bytes")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(out, ignore_errors=True)
print("   stopped      the port is free again and no run folder was left")
EOF
fi

echo
echo "== ready"

if [ -n "$VENV" ]; then
    cat <<EOF

  the install went into $VENV. Activate it before anything below, or spell the
  interpreter out as $VENV/bin/python:

      source $VENV/bin/activate
EOF
fi

cat <<'EOF'

  serve the API with a simulated rig, every settle a no-op:

      python -m bace.service --sim --fast --port 8900

  or with the console alongside it at /ui, which is the form the start-up
  check above just ran and the one you want (/ redirects there):

      python -m bace.service --sim --fast --port 8900 --ui ui/

  GET / lists every route, /docs is the interactive page, and
  bace/service/README.md walks the whole operator flow by hand.
  docs/ui-kickoff.md is where the console work starts.
EOF
