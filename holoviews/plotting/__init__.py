"""
HoloViews plotting sub-system the defines the interface to be used by
any third-party plotting/rendering package.

This file defines the HTML tags used to wrap renderered output for
display in the IPython Notebook (optional).
"""

from ..core.options import Cycle
from .plot import Plot
from .renderer import Renderer, HTML_TAGS

try:
    import matplotlib
except:
    matplotlib = None

try:
    import bokeh
except:
    bokeh = None

def public(obj):
    if not isinstance(obj, type): return False
    is_plot_or_cycle = any([issubclass(obj, bc) for bc in [Plot, Cycle]])
    is_renderer = any([issubclass(obj, bc) for bc in [Renderer]])
    return (is_plot_or_cycle or is_renderer)

if matplotlib is not None:
    from . import mpl # pyflakes:ignore (API import)
if bokeh is not None:
    from . import bokeh # pyflakes:ignore (API import)

_public = list(set([_k for _k, _v in locals().items() if public(_v)]))
__all__ = _public
