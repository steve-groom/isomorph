#!/bin/sh
# Install isomorph for this user so `from isomorph import ...` works
# with no PYTHONPATH. Same mechanism as MyHDL: pip + site-packages.
# Unlike MyHDL it also offers to install the external tools.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "$HERE/pyproject.toml")

usage () {
    cat <<'EOF'
usage: install.sh [--tar] [--pack] [--user] [--target DIR]
                  [--editable] [--deps] [--no-deps] [--dest DIR]

  default     pip-install this tree into the site-packages of the
              active python, which is where MyHDL is, then offer
              the external tools
  --tar       write isomorph-<version>.tar.gz and stop
  --pack      copy a clean tree to ~/isomorph and stop
  --user      force pip --user (~/.local) even if a venv or conda
              environment is writable
  --target DIR
              put the package in DIR instead, a tools folder of
              your choosing, and write one .pth line so Python
              still finds it with no PYTHONPATH
  --editable  pip install -e (developers; points at this tree)
  --deps      install the external tools without asking. Needs root
  --no-deps   report the external tools and never ask
  --dest DIR  where --pack copies, default ~/isomorph, and where
              --tar writes, default the current directory

After install, the house import works:

  from isomorph import (block, signal, signals, enum, always_ff,
      always_comb, assign, concat, replicate, bits, struct,
      attr, open_port, instances)
EOF
}

MODE=install
FORCE_USER=0
EDITABLE=0
WANT_DEPS=ask
DEST=
TARGET=

while [ $# -gt 0 ]; do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --tar) MODE=tar ;;
        --pack) MODE=pack ;;
        --user) FORCE_USER=1 ;;
        --target) TARGET=$2; shift ;;
        --editable) EDITABLE=1 ;;
        --deps) WANT_DEPS=yes ;;
        --no-deps) WANT_DEPS=no ;;
        --dest) DEST=$2; shift ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

if [ -n "$TARGET" ] && [ "$EDITABLE" -eq 1 ]; then
    echo "install.sh: --target and --editable are two answers to the"\
         "same question" >&2
    exit 2
fi

PYTHON=${PYTHON:-python3}
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "install.sh: $PYTHON not found" >&2
    exit 1
fi

copy_tree () {
    src=$1
    dst=$2
    mkdir -p "$dst/isomorph"
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude '__pycache__' \
            --exclude '*.pyc' \
            --exclude '*.egg-info' \
            "$src/isomorph/" "$dst/isomorph/"
        cp -f "$src/pyproject.toml" "$src/install.sh" "$src/INSTALL.txt" \
            "$dst/"
        if [ -f "$src/SPEC.txt" ]; then
            cp -f "$src/SPEC.txt" "$dst/"
        fi
    else
        (cd "$src" && tar cf - \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --exclude='*.egg-info' \
            isomorph pyproject.toml install.sh INSTALL.txt SPEC.txt) |
            (cd "$dst" && tar xf -)
    fi
    chmod +x "$dst/install.sh"
}

if [ "$MODE" = pack ]; then
    DEST=${DEST:-$HOME/isomorph}
    echo "packing $HERE -> $DEST"
    copy_tree "$HERE" "$DEST"
    echo "packed $DEST"
    exit 0
fi

if [ "$MODE" = tar ]; then
    DEST=${DEST:-$(pwd)}
    NAME=isomorph-$VERSION
    STAGE=$(mktemp -d)
    trap 'rm -rf "$STAGE"' EXIT INT TERM
    copy_tree "$HERE" "$STAGE/$NAME"
    mkdir -p "$DEST"
    tar czf "$DEST/$NAME.tar.gz" -C "$STAGE" "$NAME"
    echo "wrote $DEST/$NAME.tar.gz"
    echo
    echo "they unpack and install with"
    echo "    tar xzf $NAME.tar.gz"
    echo "    cd $NAME"
    echo "    ./install.sh"
    exit 0
fi

PIP_FLAGS="--upgrade"
if [ "$EDITABLE" -eq 1 ]; then
    PIP_FLAGS="$PIP_FLAGS -e"
fi

pip_try () {
    "$PYTHON" -m pip install $PIP_FLAGS "$@" "$HERE"
}

echo "installing isomorph $VERSION from $HERE with $PYTHON"
if [ -n "$TARGET" ]; then
    mkdir -p "$TARGET"
    TARGET=$(CDPATH= cd -- "$TARGET" && pwd)
    pip_try --target "$TARGET"
    PTH=$("$PYTHON" - "$TARGET" <<'PY'
import os
import site
import sys

places = list(getattr(site, 'getsitepackages', list)())
places.append(site.getusersitepackages())
for place in places:
    if os.path.isdir(place) and os.access(place, os.W_OK):
        break
else:
    place = site.getusersitepackages()
    os.makedirs(place, exist_ok = True)
path = os.path.join(place, 'isomorph.pth')
with open(path, 'w', encoding = 'utf-8') as f:
    f.write(sys.argv[1] + '\n')
print(path)
PY
)
    echo "found by: $PTH"
elif [ "$FORCE_USER" -eq 1 ]; then
    pip_try --user || pip_try --user --no-build-isolation
elif pip_try; then
    :
elif pip_try --user; then
    :
else
    # a machine with no network cannot fetch setuptools to build with
    echo "retrying without build isolation" >&2
    pip_try --user --no-build-isolation
fi

# from / so the tree we just installed from cannot answer instead of
# site-packages
(cd / && "$PYTHON" - ) <<'PY'
from isomorph import (block, signal, signals, enum, always_ff,
    always_comb, assign, concat, replicate, bits, struct,
    attr, open_port, instances)
import isomorph
print('isomorph ok')
print('  isomorph', isomorph.__file__)
try:
    import myhdl
    print('  myhdl   ', myhdl.__file__)
except ImportError:
    pass
PY

# Isomorph converts to SystemVerilog and VHDL and simulates in Python
# with none of these installed. gcc, verilator and ghdl switch on the
# other two simulator backends and --lint.
echo
"$PYTHON" -m isomorph doctor || true

COMMAND=
if [ "$WANT_DEPS" != no ]; then
    COMMAND=$("$PYTHON" -c \
        'import isomorph.deps as d; print(d.install_command() or "")')
fi

if [ -n "$COMMAND" ]; then
    RUN=$WANT_DEPS
    if [ "$RUN" = ask ]; then
        RUN=no
        if [ -t 0 ]; then
            printf '\ninstall the missing ones now?\n    %s\n[y/N] ' \
                "$COMMAND"
            read -r ANSWER
            case $ANSWER in
                y|Y|yes|YES) RUN=yes ;;
            esac
        fi
    fi
    if [ "$RUN" = yes ]; then
        echo "running: $COMMAND"
        sh -c "$COMMAND"
        echo
        "$PYTHON" -m isomorph doctor || true
    fi
fi

echo
echo "done. New Python sessions can use the house import."
echo "run a design:   python3 design.py --run verilator"
echo "convert:        python3 design.py --sv --vhdl --lint"
echo "check tools:    python3 -m isomorph doctor"
