"""The scripts' name for tests/conformance/procs.py (the one way a child process is started): `import procs as subprocess`.

This file only finds the real module, so that a script in this directory (which has itself first on the path) and a test (which has tests/conformance first)
get the same module object and one set of process groups to reap."""

import importlib.util
import pathlib
import sys

_real = pathlib.Path(__file__).resolve().parents[1] / "tests" / "conformance" / "procs.py"
_spec = importlib.util.spec_from_file_location("procs", _real)
_module = importlib.util.module_from_spec(_spec)
sys.modules["procs"] = _module
_spec.loader.exec_module(_module)
