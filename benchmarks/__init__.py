"""Offline experiment runners and release-verification drivers.

These modules are development tooling, not part of the installed package: they are
excluded from the wheel and from the sdist allowlist, and they may import optional
platform extras that the core runtime must not depend on.
"""
