"""Optional full CLI with a dependency-free minimal launcher."""

import sys
from importlib import import_module
from importlib.util import find_spec

_CLI_MODULES = ("fastapi", "pypdf", "PIL", "typer", "pydantic_settings", "httpx", "yaml", "uvicorn")

if all(find_spec(name) is not None for name in _CLI_MODULES):
    # Exact module alias preserves Typer registration and historical monkeypatches.
    sys.modules[__name__] = import_module("forgeharness.legacy_cli")
else:

    def app() -> None:
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
