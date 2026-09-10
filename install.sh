#!/bin/sh
# Install isomorph for this user so `from isomorph import ...` works
# with no PYTHONPATH. Same mechanism as MyHDL: pip + site-packages.
set -eu

HOME_DEST="${HOME}/isomorph"
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

usage () {
    cat <<'EOF'
usage: install.sh [--pack] [--user] [--editable] [--deps] [--dest DIR]

  default     copy a clean tree to ~/isomorph and pip-install it
  --pack      copy to ~/isomorph only (for tarball / scp)
  --user      force pip --user (~/.local) even if a venv/conda
              env is writable
  --editable  pip install -e (developers; points at the tree)
  --deps      install the external tools too (gcc, verilator,
              ghdl). Needs root. Without it they are only
              reported.
  --dest DIR  install prefix instead of ~/isomorph

After install, the house import works:

  from isomorph import (block, signal, signals, enum, always_ff,
      always_comb, assign, concat, replicate, bits, struct,
      attr, open_port, instances)
EOF
}

PACK_ONLY=0
FORCE_USER=0
EDITABLE=0
WITH_DEPS=0
DEST=$HOME_DEST

while [ $# -gt 0 ]; do
    case $1 in
        -h|--help) usage; exit 0 ;;
        --pack) PACK_ONLY=1 ;;
        --user) FORCE_USER=1 ;;
        --editable) EDITABLE=1 ;;
        --deps) WITH_DEPS=1 ;;
        --dest) DEST=$2; shift ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

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

echo "packing $HERE -> $DEST"
copy_tree "$HERE" "$DEST"

if [ "$PACK_ONLY" -eq 1 ]; then
    echo "packed $DEST"
    echo "share with: tar czf isomorph.tar.gz -C \"\$HOME\" isomorph"
    exit 0
fi

PIP_FLAGS="--upgrade"
if [ "$EDITABLE" -eq 1 ]; then
    PIP_FLAGS="$PIP_FLAGS -e"
fi

pip_install () {
    extra=$1
    "$PYTHON" -m pip install $PIP_FLAGS $extra "$DEST"
}

echo "installing with $PYTHON"
if [ "$FORCE_USER" -eq 1 ]; then
    pip_install --user
elif pip_install "" ; then
    :
else
    echo "retrying with --user (home site-packages)" >&2
    pip_install --user
fi

"$PYTHON" - <<'PY'
from isomorph import (block, signal, signals, enum, always_ff,
    always_comb, assign, concat, replicate, bits, struct,
    attr, open_port, instances)
import isomorph
print('isomorph ok')
print(' ', isomorph.__file__)
PY

# ------------------------------------------------------------------
# External tools. Isomorph converts to SystemVerilog and VHDL and
# simulates in Python with none of them installed. gcc, verilator
# and ghdl switch on the other two simulator backends and --lint.
echo
"$PYTHON" -m isomorph doctor || true

if [ "$WITH_DEPS" -eq 1 ]; then
    COMMAND=$("$PYTHON" -c \
        'import isomorph.deps as d; print(d.install_command() or "")')
    if [ -z "$COMMAND" ]; then
        echo "nothing missing, or this platform is not one install.sh"
        echo "knows how to advise on; install the tools by hand."
    else
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
