"""Run the suite against the tree it lives in, not the installed copy.

pytest puts the working directory first on sys.path, so an import in the
test process finds this tree. A test that runs a design file in a
subprocess does not inherit that, and cheerfully checks whichever
version is in site-packages instead, which is the last one installed and
not the one being edited. Putting the tree on PYTHONPATH here settles it
for both.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

parts = [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p]
if ROOT not in parts:
    os.environ['PYTHONPATH'] = os.pathsep.join([ROOT] + parts)
