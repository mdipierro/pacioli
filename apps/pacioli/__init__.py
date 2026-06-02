import os
import sys

# Put the vendored pacioli_erp package on sys.path *before* anything imports it.
# (apps/pacioli/packages/pacioli_erp/ is a self-contained repo holding pyproject.toml
# and the importable pacioli_erp/ package.)  If pacioli_erp is also pip-installed,
# that copy is found first; this insert is what lets the app run with no install.
_VENDORED = os.path.join(os.path.dirname(__file__), "packages", "pacioli_erp")
if os.path.isdir(_VENDORED) and _VENDORED not in sys.path:
    sys.path.insert(0, _VENDORED)

import py4web  # noqa: E402

if not py4web.check_compatible("1.20230101.1"):
    raise RuntimeError("py4web 1.20230101.1+ required")

from . import controllers  # noqa: E402,F401
from .models import db  # noqa: E402,F401
from .tasks import scheduler  # noqa: E402,F401

__version__ = "0.1.0"
__author__ = "Massimo Di Pierro"
__license__ = "BSD-3-Clause"
