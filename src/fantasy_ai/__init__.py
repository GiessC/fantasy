"""Local fantasy football draft analysis.

Layering (see ARCHITECTURE.md):

    sources -> normalization -> db -> analytics -> draft -> llm -> cli

Nothing below a layer may import from a layer above it.  In particular the
analytics package must never import ``fantasy_ai.sources`` or ``fantasy_ai.llm``.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
