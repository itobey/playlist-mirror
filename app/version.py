"""The declared version of this application, and the single source of truth for it."""

import os

# Written by the release workflow - do not hand-edit.
__version__ = "1.0.0"

# Set at image build time for builds that are not releases, so a master build is
# distinguishable from the release it descends from. PEP 440 local-version
# syntax: a release reports "0.1.0", a master build "0.1.0+master.a1b2c3d".
#
# Without this a committed constant would report the same string for every build
# between two releases, which in a telemetry row is worse than useless — it says
# "0.1.0" for code that is not 0.1.0.
_BUILD = os.environ.get("APP_BUILD", "").strip()

VERSION = f"{__version__}+{_BUILD}" if _BUILD else __version__
