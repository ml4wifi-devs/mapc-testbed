#!/usr/bin/env python3
"""Modules deployed to testbed nodes must parse and run under Python 3.4.0.

The AP image is Ubuntu 14.04 with Python 3.4.0 and no pip, so a 3.5+ construct in a
node-deployed module is not a test failure on the controller -- it is a SyntaxError on the rig,
mid-experiment, after deployment. This test catches it here instead.

It is a static AST scan, so it works regardless of which Python actually runs the suite.
"""
import ast
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Taken from the deployer rather than restated, so a module added to the deployment cannot
# escape this scan by being forgotten here.
sys.path.insert(0, ROOT)
from cosr import deploy as _deploy

NODE_MODULES = list(_deploy.MODULES)

# name -> why it is unavailable on 3.4
BANNED_CALLS = {
    "subprocess.run": "added in 3.5; use check_output/Popen",
    "os.set_blocking": "added in 3.5; use fcntl",
    "math.isclose": "added in 3.5",
    "time.time_ns": "added in 3.7",
    "subprocess.DEVNULL": "",           # actually 3.3, kept as a reminder slot
}
del BANNED_CALLS["subprocess.DEVNULL"]

BANNED_MODULES = {
    "asyncio": "3.4 asyncio predates async/await and is not worth the risk on the AP image",
    "dataclasses": "added in 3.7",
    "typing": "unnecessary on nodes; 3.4's typing is absent",
    "secrets": "added in 3.6",
    "pathlib": "3.4 pathlib lacks most of the modern API",
}


def node_module_paths():
    out = []
    for name in NODE_MODULES:
        p = os.path.join(ROOT, "cosr", name)
        if os.path.exists(p):
            out.append((name, p))
    return out


class TestPy34Compat(unittest.TestCase):

    def setUp(self):
        self.modules = node_module_paths()
        self.assertTrue(self.modules, "no node modules found to check")

    def _tree(self, path):
        with open(path, "r") as fh:
            return ast.parse(fh.read(), filename=path)

    def test_no_fstrings(self):
        """f-strings are 3.6+. Use % formatting."""
        for name, path in self.modules:
            for node in ast.walk(self._tree(path)):
                if node.__class__.__name__ == "JoinedStr":
                    self.fail("%s:%d uses an f-string (3.6+); use %% formatting"
                              % (name, getattr(node, "lineno", 0)))

    def test_no_async_syntax(self):
        """async def / await / async for / async with are 3.5+."""
        banned = ("AsyncFunctionDef", "Await", "AsyncFor", "AsyncWith")
        for name, path in self.modules:
            for node in ast.walk(self._tree(path)):
                if node.__class__.__name__ in banned:
                    self.fail("%s:%d uses %s (3.5+)"
                              % (name, getattr(node, "lineno", 0),
                                 node.__class__.__name__))

    def test_no_matrix_multiply_or_starred_literals(self):
        """@ operator is 3.5+; PEP 448 extended unpacking in literals is 3.5+."""
        for name, path in self.modules:
            tree = self._tree(path)
            for node in ast.walk(tree):
                if node.__class__.__name__ == "MatMult":
                    self.fail("%s: uses the @ operator (3.5+)" % name)
                if isinstance(node, ast.Dict) and any(k is None for k in node.keys):
                    self.fail("%s:%d uses {**a} dict unpacking (3.5+)"
                              % (name, getattr(node, "lineno", 0)))
                if isinstance(node, ast.Call):
                    for kw in node.keywords or []:
                        if kw.arg is None and not isinstance(node.func, ast.Attribute):
                            pass    # **kwargs in a call is fine; {**a} literals are not

    def test_no_banned_calls(self):
        for name, path in self.modules:
            for node in ast.walk(self._tree(path)):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                    dotted = "%s.%s" % (f.value.id, f.attr)
                    if dotted in BANNED_CALLS:
                        self.fail("%s:%d calls %s -- %s"
                                  % (name, getattr(node, "lineno", 0), dotted,
                                     BANNED_CALLS[dotted]))

    def test_no_banned_imports(self):
        for name, path in self.modules:
            for node in ast.walk(self._tree(path)):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module.split(".")[0]]
                for m in mods:
                    if m in BANNED_MODULES:
                        self.fail("%s:%d imports %s -- %s"
                                  % (name, getattr(node, "lineno", 0), m,
                                     BANNED_MODULES[m]))

    def test_no_variable_annotations(self):
        """PEP 526 `x: int = 1` is 3.6+."""
        for name, path in self.modules:
            for node in ast.walk(self._tree(path)):
                if node.__class__.__name__ == "AnnAssign":
                    self.fail("%s:%d uses a variable annotation (3.6+)"
                              % (name, getattr(node, "lineno", 0)))

    def test_no_function_annotations(self):
        """Harmless syntactically on 3.4 but a signal the module drifted; keep node code plain."""
        for name, path in self.modules:
            for node in ast.walk(self._tree(path)):
                if isinstance(node, (ast.FunctionDef,)):
                    if node.returns is not None or any(
                            a.annotation is not None for a in node.args.args):
                        self.fail("%s:%d annotates a signature; keep node modules plain"
                                  % (name, getattr(node, "lineno", 0)))

    def test_no_underscore_numeric_literals(self):
        """1_000 is 3.6+. AST normalises it, so scan the source text."""
        import re
        pattern = re.compile(r"\b\d+_\d")
        for name, path in self.modules:
            with open(path) as fh:
                for i, line in enumerate(fh, 1):
                    code = line.split("#", 1)[0]
                    if pattern.search(code):
                        self.fail("%s:%d uses an underscore numeric literal (3.6+)"
                                  % (name, i))

    def test_modules_actually_import(self):
        """A syntax-clean module that cannot import is equally broken on the rig."""
        sys.path.insert(0, ROOT)
        for name, _path in self.modules:
            mod = "cosr." + name[:-3]
            try:
                __import__(mod)
            except ImportError as e:
                # nl80211/agent may need Linux-only symbols; a controller cannot import those
                if "AF_NETLINK" in str(e) or "AF_PACKET" in str(e):
                    continue
                raise

if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestTheDigestCoversWhatIsShipped(unittest.TestCase):
    """A module deployed but left out of the digest lets a node with a stale copy of it pass
    verification, which is the one thing the digest exists to prevent."""

    def test_every_deployed_module_is_hashed(self):
        import re
        src = open(os.path.join(ROOT, "cosr", "agent.py")).read()
        block = re.search(r"for name in sorted\(\((.*?)\)\):", src, re.S).group(1)
        hashed = set(re.findall(r'"([^"]+\.py)"', block))
        self.assertEqual(hashed, set(_deploy.MODULES),
                         "cosr.agent.source_hash and cosr.deploy.MODULES disagree")
