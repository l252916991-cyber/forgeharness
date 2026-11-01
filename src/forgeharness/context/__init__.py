"""Budgeted context selection, compaction, and repository indexing."""

from forgeharness.context.assembly import AssembledContext, ContextAssembler
from forgeharness.context.compiler import CompiledContext, ContextCompiler, ContextItem

__all__ = [
    "AssembledContext",
    "CompiledContext",
    "ContextAssembler",
    "ContextCompiler",
    "ContextItem",
    "PythonRepositoryMap",
]


def __getattr__(name: str) -> object:
    if name == "PythonRepositoryMap":
        from importlib import import_module

        return import_module("forgeharness.coding.repository").PythonRepositoryMap
    raise AttributeError(name)
