#!/bin/sh
# Install isomorph for this user so `from isomorph import ...` works
# with no PYTHONPATH. Same mechanism as MyHDL: pip + site-packages.
# Unlike MyHDL it also offers to install the external tools.
set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO=${ISOMORPH_REPO:-https://github.com/steve-groom/isomorph}
BRANCH=${ISOMORPH_BRANCH:-main}

# curl | sh arrives with no tree to install from, so fetch one and
# hand the arguments to the copy inside it. Reading stdin from the
# terminal, because the pipe this came down is not one
if [ ! -f "$HERE/isomorph/__init__.py" ]; then
    TMP=$(mktemp -d)
    trap 'rm -rf "$TMP"' EXIT INT TERM
    URL=$REPO/archive/refs/heads/$BRANCH.tar.gz
    echo "fetching $URL"
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL "$URL" | (cd "$TMP" && tar xzf -)
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- "$URL" | (cd "$TMP" && tar xzf -)
    else
        echo "install.sh: needs curl or wget to fetch isomorph" >&2
        exit 1
    fi
    if (exec < /dev/tty) 2>/dev/null; then
        sh "$TMP/isomorph-$BRANCH/install.sh" "$@" < /dev/tty
    else
        sh "$TMP/isomorph-$BRANCH/install.sh" "$@"
    fi
    exit 0
fi

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

if ! "$PYTHON" -c 'import sys; sys.exit(sys.version_info[:2] < (3, 11))'
then
    echo "install.sh: isomorph needs Python 3.11 or newer, and" >&2
    "$PYTHON" -c 'import sys; print(" ", sys.executable, sys.version)' >&2
    echo "  Mint 22 and Ubuntu 24.04 have 3.12; Mint 21 has 3.10." >&2
    echo "  PYTHON=/path/to/python3.12 ./install.sh uses another." >&2
    exit 1
fi

# --deps and --no-deps answer for every one of these, not only the
# EDA tools
ask_and_run () {
    echo "    $1"
    if [ "$WANT_DEPS" = no ]; then
        return 1
    fi
    if [ "$WANT_DEPS" != yes ]; then
        if [ ! -t 0 ]; then
            return 1
        fi
        printf '  Run it? [y/N] '
        read -r ANSWER
        case $ANSWER in
            y|Y|yes|YES) ;;
            *) return 1 ;;
        esac
    fi
    sh -c "$1"
}

if ! "$PYTHON" -m pip --version >/dev/null 2>&1; then
    echo "$PYTHON has no pip, and pip is how isomorph installs."
    PIP_COMMAND=$("$PYTHON" - "$HERE" <<'PY'
import sys

sys.path.insert(0, sys.argv[1])
from isomorph.deps import pip_command
print(pip_command() or '')
PY
)
    if [ -z "$PIP_COMMAND" ] || ! ask_and_run "$PIP_COMMAND"; then
        echo "install.sh: install pip for $PYTHON, then run this again" >&2
        exit 1
    fi
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
            "$src/README.md" "$src/LICENSE" "$dst/"
        if [ -f "$src/SPEC.txt" ]; then
            cp -f "$src/SPEC.txt" "$dst/"
        fi
    else
        (cd "$src" && tar cf - \
            --exclude='__pycache__' \
            --exclude='*.pyc' \
            --exclude='*.egg-info' \
            isomorph pyproject.toml install.sh INSTALL.txt \
            README.md LICENSE SPEC.txt) |
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

# Ask where this python will let us write before asking pip to do
# it. Mint and Ubuntu mark theirs externally managed (PEP 668) and
# refuse a system install; discovering that by trying costs the
# reader two screens of error for something we can just read
WHERE=$("$PYTHON" - <<'PY'
import os
import sys
import sysconfig

conda = os.environ.get('CONDA_PREFIX')
if sys.prefix != sys.base_prefix or (conda and sys.prefix == conda):
    print('env')
elif os.path.exists(os.path.join(sysconfig.get_path('stdlib'),
                                 'EXTERNALLY-MANAGED')):
    print('managed')
else:
    print('system')
PY
)

FLAGS=
if [ "$WHERE" = managed ]; then
    FLAGS="--user --break-system-packages"
elif [ "$FORCE_USER" -eq 1 ]; then
    FLAGS="--user"
fi

if [ "$WHERE" = managed ]; then
    cat <<EOF

This python is externally managed: the distribution owns it and
pip will not add to its site-packages.

    $PYTHON

Isomorph can go into your home site-packages instead, which works
and leaves the system python alone, but a conda or venv
environment is the better answer. That is where MyHDL goes, the
isomorph command lands on your PATH, and nothing sits beside the
packages apt manages.

Miniconda, if you want one:

    curl -fsSL -o miniconda.sh \\
        https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
    bash miniconda.sh -b -p \$HOME/miniconda3
    \$HOME/miniconda3/bin/conda init bash

Then open a new shell and run this install again.

EOF
    if [ "$FORCE_USER" -eq 0 ] && [ "$WANT_DEPS" != yes ] && [ -t 0 ]; then
        printf 'Install into your home site-packages anyway? [y/N] '
        read -r ANSWER
        case $ANSWER in
            y|Y|yes|YES) ;;
            *) echo "nothing installed."; exit 0 ;;
        esac
    fi
fi

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
elif [ -n "$FLAGS" ]; then
    # a machine with no network cannot fetch setuptools to build with
    pip_try $FLAGS || pip_try $FLAGS --no-build-isolation
elif pip_try; then
    :
elif pip_try --user; then
    :
else
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

import os
import shutil
import sysconfig

if shutil.which('isomorph') is None:
    for scripts in (sysconfig.get_path('scripts', 'posix_user'),
                    sysconfig.get_path('scripts')):
        if os.path.exists(os.path.join(scripts, 'isomorph')):
            print()
            print(f'  the isomorph command is in {scripts},')
            print('  which is not on this PATH. python3 -m isomorph')
            print('  works anyway, and on Debian and its family a new')
            print('  login adds that directory now that it exists.')
            break
PY

# Isomorph converts to SystemVerilog and VHDL and simulates in Python
# with none of these installed. gcc, verilator and ghdl switch on the
# other two simulator backends and --lint. doctor does the asking, so
# a pip install straight from git gets the same offer this does.
echo
case $WANT_DEPS in
    yes) "$PYTHON" -m isomorph doctor --install --yes || true ;;
    ask) "$PYTHON" -m isomorph doctor --install || true ;;
    *)   "$PYTHON" -m isomorph doctor || true ;;
esac

echo
echo "done. New Python sessions can use the house import."
echo "run a design:   python3 design.py --run verilator"
echo "convert:        python3 design.py --sv --vhdl --lint"
echo "check tools:    python3 -m isomorph doctor"
