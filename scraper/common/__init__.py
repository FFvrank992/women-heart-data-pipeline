"""Shared foundation for every scraper in this project.

Four modules, one job each:

    io_utils   encoding-safe CSV reading and atomic writing
    pii        the single PII engine -- one policy for all six platforms
    schema     the five-column contract: vocabulary, dates, dedupe, validation
    sources    the registry of the six scrapers, and the harvest step

Every scraper imports from here rather than rolling its own cleaning, so the
six platforms cannot drift apart and the PII policy can be audited in one file.

Submodules load lazily, on first attribute access. That is not premature
optimisation: `pii` is pure standard library, while `io_utils`, `schema` and
`sources` need pandas. The heart_sisters scraper is deliberately stdlib-only
and ships an offline test suite, so `from common import pii` must not drag
pandas in behind it.
"""

import importlib

__all__ = ["io_utils", "pii", "schema", "sources"]


def __getattr__(name: str):
    if name in __all__:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)
