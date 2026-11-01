"""Deprecated coding repository-map import path; use coding.repository."""

from importlib import import_module
from typing import Any


def __getattr__(name: str) -> Any:
    return getattr(import_module("forgeharness.coding.repository"), name)
