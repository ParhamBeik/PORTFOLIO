"""The view package's shape, pinned.

`portfolio/views.py` was one 2,871-line module; it is now a package split along
the groupings `urls.py` already used. That split was pure code movement, and
the risk in pure code movement is silent loss -- a name that stops being
exported, an endpoint that quietly stops resolving, or a module that grows an
import cycle nobody notices until a worker boots in a different order.

These are cheap structural assertions, not behaviour tests. Every endpoint's
behaviour is covered by the suites named for its concern.
"""
import importlib
import sys

import pytest
from django.urls import get_resolver

SUBMODULES = ("_common", "admin_ops", "analytics", "catalog", "ledger", "valuation")


def _api_endpoints():
    """(route, view class) for every `api/` URL served by portfolio.views."""
    found = []

    def walk(patterns, prefix=""):
        for pattern in patterns:
            if hasattr(pattern, "url_patterns"):
                walk(pattern.url_patterns, prefix + str(pattern.pattern))
                continue
            view = getattr(pattern.callback, "view_class", None) or getattr(
                pattern.callback, "cls", None
            )
            if view is not None and view.__module__.startswith("portfolio.views"):
                found.append((prefix + str(pattern.pattern), view))

    walk(get_resolver().url_patterns)
    return found


def test_every_portfolio_endpoint_still_resolves():
    """The split must not have stranded a route on a name that moved."""
    endpoints = _api_endpoints()
    assert len(endpoints) >= 40, (
        f"only {len(endpoints)} portfolio endpoints resolve; the package is "
        "not exporting everything urls.py asks for"
    )
    # Nothing may still be served out of a module-level `views.py`: that file is
    # gone, and an import resolving to it would mean a stale .pyc or a
    # half-finished move.
    for route, view in endpoints:
        assert view.__module__ != "portfolio.views", (
            f"{route} -> {view.__name__} resolves to the package root rather "
            "than a concern module"
        )


def test_public_names_are_re_exported_from_the_package_root():
    """`urls.py` and every other importer must see the pre-split surface.

    The package root is the compatibility layer. If a name defined in a concern
    module is public but missing here, an importer that predates the split
    breaks -- and it breaks at import time, on a worker, not in a test.
    """
    import portfolio.views as package

    missing = []
    for name in SUBMODULES:
        module = importlib.import_module(f"portfolio.views.{name}")
        for attr in vars(module):
            if attr.startswith("_"):
                continue
            value = getattr(module, attr)
            # only names this module actually defines, not what it imported
            if getattr(value, "__module__", None) != module.__name__:
                continue
            if not hasattr(package, attr):
                missing.append(f"{name}.{attr}")
    assert not missing, f"defined but not re-exported: {sorted(missing)}"


@pytest.mark.parametrize("name", SUBMODULES)
def test_each_module_imports_on_its_own(name):
    """No module may depend on a sibling having been imported first.

    Django imports views in whatever order the URL conf reaches them, and a
    Celery worker may never import some of them at all. A cycle that happens to
    resolve when the package root is imported first would fail there instead.
    """
    for loaded in [k for k in sys.modules if k.startswith("portfolio.views")]:
        del sys.modules[loaded]
    importlib.import_module(f"portfolio.views.{name}")
