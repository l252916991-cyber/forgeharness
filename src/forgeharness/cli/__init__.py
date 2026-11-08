"""The ``forge`` command.

With the ``cli`` extra installed, ``app`` is the full Typer application in
``forgeharness.cli.commands``. A core install gets a dependency-free launcher that
prints SDK help and names the extra each command needs.
"""

from importlib.util import find_spec

_CLI_MODULES = ("fastapi", "pypdf", "PIL", "typer", "pydantic_settings", "httpx", "yaml", "uvicorn")
FULL_CLI = all(find_spec(name) is not None for name in _CLI_MODULES)

if FULL_CLI:
    from forgeharness.cli.commands import app
else:

    def app() -> None:  # type: ignore[misc]  # the core-install launcher
        """Print minimal SDK help or an actionable optional-extra error."""
        import argparse

        from forgeharness import __version__

        parser = argparse.ArgumentParser(
            prog="forge",
            description="ForgeHarness embedded SDK v1 (core install: SDK only)",
            epilog=(
                "The full command set needs the optional dependencies: "
                'pip install "forgeharness[cli]"'
            ),
        )
        parser.add_argument(
            "command",
            nargs="?",
            help="full command set requires: pip install 'forgeharness[cli]'",
        )
        parser.add_argument("--version", action="version", version=f"forgeharness {__version__}")
        args = parser.parse_args()
        if args.command:
            parser.error(
                f"`{args.command}` needs the optional CLI dependencies: "
                "pip install 'forgeharness[cli]'"
            )
        parser.print_help()
