"""Shipped same-origin assets for the invitation-only internal workbench."""

from importlib.resources import files


def _asset(name: str) -> str:
    return files("forgeharness.cloud").joinpath("assets", name).read_text(encoding="utf-8")


WORKBENCH_HTML = _asset("workbench.html")
WORKBENCH_SCRIPT = _asset("workbench.js")
WORKBENCH_CSS = _asset("workbench.css")
