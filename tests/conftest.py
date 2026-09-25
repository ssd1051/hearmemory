import os
import tempfile

# macOS: the default temp dir lives under /var, a symlink to /private/var. The code resolves project
# roots, so tests that compare paths must start from the resolved temp dir too.
tempfile.tempdir = os.path.realpath(tempfile.gettempdir())

# Tests must not depend on (or touch) the developer's own setup, e.g. a global core.hooksPath set by
# another tool: give the test run an empty HOME and no system-level git config. Tests that need a
# user-level git config still patch HOME themselves.
_HOME = tempfile.mkdtemp(prefix="hearmemory-test-home-")
os.environ["HOME"] = _HOME
os.environ["XDG_CONFIG_HOME"] = os.path.join(_HOME, ".config")
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
