"""Coding reference application; legacy skills are loaded only when requested."""

from typing import Any

__all__ = ["CodingAgent"]


def __getattr__(name: str) -> Any:
    if name == "CodingAgent":
        from importlib import import_module

        return import_module("forgeharness.coding.agent").CodingAgent
    raise AttributeError(name)
