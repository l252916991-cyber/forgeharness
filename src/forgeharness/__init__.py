"""ForgeHarness public package."""

from importlib.metadata import PackageNotFoundError, version

from forgeharness.runtime.loop import AgentRuntime


def _installed_version() -> str:
    """Report the installed distribution's version, not a hand-edited constant.

    The release number lives in exactly one place — ``pyproject.toml`` — and the
    built artifact carries it. Reading it back means ``forgeharness.__version__``
    cannot drift from the wheel the user actually installed.
    """
    try:
        return version("forgeharness")
    except PackageNotFoundError:  # running from a source tree that was never built
        return "0.0.0+source"


__version__ = _installed_version()

__all__ = ["AgentRuntime", "__version__"]
