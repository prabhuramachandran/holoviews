"""
Public API for all plots supported by HoloViews, regardless of
plotting package or backend. Every plotting classes must be a subclass
of this Plot baseclass.
"""

from itertools import groupby, product
from collections import Counter

import numpy as np
import param

from ..core import OrderedDict
from ..core import util, traversal
from ..core.element import HoloMap, Element, GridSpace
from ..core.overlay import Overlay, CompositeOverlay
from ..core.layout import Empty, NdLayout, Layout
from ..core.options import Store, Compositor
from ..core.util import safe_unicode
from ..element import Table, Annotation


class Plot(param.Parameterized):
    """
    Base class of all Plot classes in HoloViews, designed to be
    general enough to use any plotting package or backend.
    """

    # A list of style options that may be supplied to the plotting
    # call
    style_opts = []

    def initialize_plot(self, ranges=None):
        """
        Initialize the matplotlib figure.
        """
        raise NotImplementedError


    def update(self, key):
        """
        Update the internal state of the Plot to represent the given
        key tuple (where integers represent frames). Returns this
        state.
        """
        return self.state

    @property
    def state(self):
        """
        The plotting state that gets updated via the update method and
        used by the renderer to generate output.
        """
        raise NotImplementedError


    def __len__(self):
        """
        Returns the total number of available frames.
        """
        raise NotImplementedError

    @classmethod
    def lookup_options(cls, obj, group):
        return Store.lookup_options(cls.renderer.backend, obj, group)


class PlotSelector(object):
    """
    Proxy that allows dynamic selection of a plotting class based on a
    function of the plotted object. Behaves like a Plot class and
    presents the same parameterized interface.
    """
    def __init__(self, selector, plot_classes):
        """
        The selector function accepts a component instance and returns
        the appropriate key to index plot_classes dictionary.
        """
        self.selector = selector
        self.plot_classes = OrderedDict(plot_classes)
        interface = self._define_interface(self.plot_classes.values())
        self.style_opts, self.plot_options = interface


    def _define_interface(self, plots):
        parameters = [{k:v.precedence for k,v in plot.params().items()
                       if ((v.precedence is None) or (v.precedence >= 0))}
                      for plot in plots]
        param_sets = [set(params.keys()) for params in parameters]
        if not all(pset == param_sets[0] for pset in param_sets):
            raise Exception("All selectable plot classes must have identical plot options.")
        styles= [plot.style_opts for plot in plots]

        if not all(style == styles[0] for style in styles):
            raise Exception("All selectable plot classes must have identical style options.")
        return styles[0], parameters[0]


    def __call__(self, obj, **kwargs):
        key = self.selector(obj)
        if key not in self.plot_classes:
            msg = "Key %s returned by selector not in set: %s"
            raise Exception(msg  % (key, ', '.join(self.plot_classes.keys())))
        return self.plot_classes[key](obj, **kwargs)


    def __setattr__(self, label, value):
        try:
            return super(PlotSelector, self).__setattr__(label, value)
        except:
            raise Exception("Please set class parameters directly on classes %s"
                            % ', '.join(str(cls) for cls in self.__dict__['plot_classes'].values()))

    def params(self):
        return self.plot_options



class DimensionedPlot(Plot):
    """
    DimensionedPlot implements a number of useful methods
    to compute dimension ranges and titles containing the
    dimension values.
    """

    show_title = param.Boolean(default=True, doc="""
        Whether to display the plot title.""")

    title_format = param.String(default="{label} {group}", doc="""
        The formatting string for the title of this plot.""")

    normalize = param.Boolean(default=True, doc="""
        Whether to compute ranges across all Elements at this level
        of plotting. Allows selecting normalization at different levels
        for nested data containers.""")

    projection = param.ObjectSelector(default=None)

    def __init__(self, keys=None, dimensions=None, layout_dimensions=None,
                 uniform=True, subplot=False, adjoined=None, layout_num=0,
                 style=None, subplots=None, **params):
        self.subplots = subplots
        self.adjoined = adjoined
        self.dimensions = dimensions
        self.layout_num = layout_num
        self.layout_dimensions = layout_dimensions
        self.subplot = subplot
        self.keys = keys
        self.uniform = uniform
        self.drawn = False
        self.handles = {}
        super(DimensionedPlot, self).__init__(**params)


    def _get_frame(self, key):
        """
        Required on each MPLPlot type to get the data corresponding
        just to the current frame out from the object.
        """
        pass


    def _frame_title(self, key, group_size=2):
        """
        Returns the formatted dimension group strings
        for a particular frame.
        """
        if self.layout_dimensions is not None:
            dimensions, key = zip(*self.layout_dimensions.items())
        elif not self.uniform or len(self) == 1 or self.subplot:
            return ''
        else:
            key = key if isinstance(key, tuple) else (key,)
            dimensions = self.dimensions
        dimension_labels = [dim.pprint_value_string(k) for dim, k in
                            zip(dimensions, key)]
        groups = [', '.join(dimension_labels[i*group_size:(i+1)*group_size])
                  for i in range(len(dimension_labels))]
        return '\n '.join(g for g in groups if g)


    def compute_ranges(self, obj, key, ranges):
        """
        Given an object, a specific key and the normalization options
        this method will find the specified normalization options on
        the appropriate OptionTree, group the elements according to
        the selected normalization option (i.e. either per frame or
        over the whole animation) and finally compute the dimension
        ranges in each group. The new set of ranges is returned.
        """
        all_table = all(isinstance(el, Table) for el in obj.traverse(lambda x: x, [Element]))
        if obj is None or not self.normalize or all_table:
            return OrderedDict()
        # Get inherited ranges
        ranges = {} if ranges is None else dict(ranges)

        # Get element identifiers from current object and resolve
        # with selected normalization options
        norm_opts = self._get_norm_opts(obj)

        # Traverse displayed object if normalization applies
        # at this level, and ranges for the group have not
        # been supplied from a composite plot
        elements = []
        return_fn = lambda x: x if isinstance(x, Element) else None
        for group, (axiswise, framewise) in norm_opts.items():
            if group in ranges:
                continue # Skip if ranges are already computed
            elif not framewise: # Traverse to get all elements
                elements = obj.traverse(return_fn, [group])
            elif key is not None: # Traverse to get elements for each frame
                elements = self._get_frame(key).traverse(return_fn, [group])
            if not axiswise or ((not framewise or len(elements) == 1)
                                and isinstance(obj, HoloMap)): # Compute new ranges
                self._compute_group_range(group, elements, ranges)
        return ranges


    def _get_norm_opts(self, obj):
        """
        Gets the normalization options for a LabelledData object by
        traversing the object for to find elements and their ids.
        The id is then used to select the appropriate OptionsTree,
        accumulating the normalization options into a dictionary.
        Returns a dictionary of normalization options for each
        element in the tree.
        """
        norm_opts = {}

        # Get all elements' type.group.label specs and ids
        type_val_fn = lambda x: (x.id, (type(x).__name__, util.sanitize_identifier(x.group, escape=False),
                                        util.sanitize_identifier(x.label, escape=False))) \
            if isinstance(x, Element) else None
        element_specs = {(idspec[0], idspec[1]) for idspec in obj.traverse(type_val_fn)
                         if idspec is not None}

        # Group elements specs by ID and override normalization
        # options sequentially
        key_fn = lambda x: -1 if x[0] is None else x[0]
        id_groups = groupby(sorted(element_specs, key=key_fn), key_fn)
        for gid, element_spec_group in id_groups:
            gid = None if gid == -1 else gid
            group_specs = [el for _, el in element_spec_group]

            backend = self.renderer.backend
            optstree = Store.custom_options(
                backend=backend).get(gid, Store.options(backend=backend))
            # Get the normalization options for the current id
            # and match against customizable elements
            for opts in optstree:
                path = tuple(opts.path.split('.')[1:])
                applies = any(path == spec[:i] for spec in group_specs
                              for i in range(1, 4))
                if applies and 'norm' in opts.groups:
                    nopts = opts['norm'].options
                    if 'axiswise' in nopts or 'framewise' in nopts:
                        norm_opts.update({path: (nopts.get('axiswise', False),
                                                 nopts.get('framewise', False))})
        element_specs = [spec for eid, spec in element_specs]
        norm_opts.update({spec: (False, False) for spec in element_specs
                          if not any(spec[:i] in norm_opts.keys() for i in range(1, 4))})
        return norm_opts


    @staticmethod
    def _compute_group_range(group, elements, ranges):
        # Iterate over all elements in a normalization group
        # and accumulate their ranges into the supplied dictionary.
        elements = [el for el in elements if el is not None]
        group_ranges = OrderedDict()
        for el in elements:
            if isinstance(el, (Empty, Table)): continue
            for dim in el.dimensions(label=True):
                dim_range = el.range(dim)
                if dim not in group_ranges:
                    group_ranges[dim] = []
                group_ranges[dim].append(dim_range)
        ranges[group] = OrderedDict((k, util.max_range(v)) for k, v in group_ranges.items())


    @classmethod
    def _deep_options(cls, obj, opt_type, opts, specs=None):
        """
        Traverses the supplied object getting all options
        in opts for the specified opt_type and specs
        """
        lookup = lambda x: ((type(x).__name__, x.group, x.label),
                            {o: cls.lookup_options(x, opt_type).options.get(o, None)
                             for o in opts})
        return dict(obj.traverse(lookup, specs))


    def update(self, key):
        if len(self) == 1 and key == 0 and not self.drawn:
            return self.initialize_plot()
        return self.__getitem__(key)


    def __len__(self):
        """
        Returns the total number of available frames.
        """
        return len(self.keys)



class GenericElementPlot(DimensionedPlot):
    """
    Plotting baseclass to render contents of an Element. Implements
    methods to get the correct frame given a HoloMap, axis labels and
    extents and titles.
    """

    apply_ranges = param.Boolean(default=True, doc="""
        Whether to compute the plot bounds from the data itself.""")

    apply_extents = param.Boolean(default=True, doc="""
        Whether to apply extent overrides on the Elements""")

    def __init__(self, element, keys=None, ranges=None, dimensions=None,
                 overlaid=0, cyclic_index=0, zorder=0, style=None, **params):
        self.zorder = zorder
        self.cyclic_index = cyclic_index
        self.overlaid = overlaid
        if not isinstance(element, HoloMap):
            self.map = HoloMap(initial_items=(0, element),
                               kdims=['Frame'], id=element.id)
        else:
            self.map = element
        self.style = self.lookup_options(self.map.last, 'style') if style is None else style
        dimensions = self.map.kdims if dimensions is None else dimensions
        keys = keys if keys else list(self.map.data.keys())
        plot_opts = self.lookup_options(self.map.last, 'plot').options
        super(GenericElementPlot, self).__init__(keys=keys, dimensions=dimensions, **dict(params, **plot_opts))


    def _get_frame(self, key):
        if self.uniform:
            if not isinstance(key, tuple): key = (key,)
            kdims = [d.name for d in self.map.kdims]
            if self.dimensions is None:
                dimensions = kdims
            else:
                dimensions = [d.name for d in self.dimensions]
            if kdims == ['Frame'] and kdims != dimensions:
                select = dict(Frame=0)
            else:
                select = {d: key[dimensions.index(d)]
                          for d in kdims}
        elif isinstance(key, int):
            return self.map.values()[min([key, len(self.map)-1])]
        else:
            select = dict(zip(self.map.dimensions('key', label=True), key))
        try:
            selection = self.map.select((HoloMap,), **select)
        except KeyError:
            selection = None
        return selection.last if isinstance(selection, HoloMap) else selection


    def get_extents(self, view, ranges):
        """
        Gets the extents for the axes from the current View. The globally
        computed ranges can optionally override the extents.
        """
        num = 6 if self.projection == '3d' else 4
        if self.apply_ranges:
            if ranges:
                dims = view.dimensions()
                x0, x1 = ranges[dims[0].name]
                y0, y1 = ranges[dims[1].name]
                if self.projection == '3d':
                    z0, z1 = ranges[dims[2].name]
            else:
                x0, x1 = view.range(0)
                y0, y1 = view.range(1)
                if self.projection == '3d':
                    z0, z1 = view.range(2)
            if self.projection == '3d':
                range_extents = (x0, y0, z0, x1, y1, z1)
            else:
                range_extents = (x0, y0, x1, y1)
        else:
            range_extents = (np.NaN,) * num

        if self.apply_extents:
            norm_opts = self.lookup_options(view, 'norm').options
            if norm_opts.get('framewise', False):
                extents = view.extents
            else:
                extent_list = self.map.traverse(lambda x: x.extents, [Element])
                extents = util.max_extents(extent_list, self.projection == '3d')
        else:
            extents = (np.NaN,) * num
        return tuple(l1 if l2 is None or not np.isfinite(l2) else
                     l2 for l1, l2 in zip(range_extents, extents))


    def _axis_labels(self, view, subplots, xlabel, ylabel, zlabel):
        # Axis labels
        dims = view.dimensions()
        if isinstance(view, CompositeOverlay):
            dims = dims[view.ndims:]
        if dims and xlabel is None:
            xlabel = str(dims[0])
        if len(dims) >= 2 and ylabel is None:
            ylabel = str(dims[1])
        if self.projection == '3d' and len(dims) >= 3 and zlabel is None:
            zlabel = str(dims[2])
        return xlabel, ylabel, zlabel


    def _format_title(self, key):
        frame = self._get_frame(key)
        if frame is None: return None
        type_name = type(frame).__name__
        group = frame.group if frame.group != type_name else ''
        label = frame.label
        if self.layout_dimensions:
            title = ''
        else:
            title_format = util.safe_unicode(self.title_format)
            title = title_format.format(label=util.safe_unicode(label),
                                        group=util.safe_unicode(group),
                                        type=type_name)
        dim_title = self._frame_title(key, 2)
        if not title or title.isspace():
            return dim_title
        elif not dim_title or dim_title.isspace():
            return title
        else:
            return '\n'.join([title, dim_title])


    def update_frame(self, key, ranges=None):
        """
        Set the plot(s) to the given frame number.  Operates by
        manipulating the matplotlib objects held in the self._handles
        dictionary.

        If n is greater than the number of available frames, update
        using the last available frame.
        """


class GenericOverlayPlot(GenericElementPlot):
    """
    Plotting baseclass to render (Nd)Overlay objects. It implements
    methods to handle the creation of ElementPlots, coordinating style
    groupings and zorder for all layers across a HoloMap. It also
    allows collapsing of layers via the Compositor.
    """

    show_legend = param.Boolean(default=False, doc="""
        Whether to show legend for the plot.""")

    style_grouping = param.Integer(default=2,
                                   doc="""The length of the type.group.label
        spec that will be used to group Elements into style groups, i.e.
        a style_grouping value of 1 will group just by type, a value of 2
        will group by type and group and a value of 3 will group by the
        full specification.""")

    _passed_handles = []

    def __init__(self, overlay, ranges=None, **params):
        super(GenericOverlayPlot, self).__init__(overlay, ranges=ranges, **params)

        # Apply data collapse
        self.map = Compositor.collapse(self.map, None, mode='data')
        self.map = self._apply_compositor(self.map, ranges, self.keys)
        self.subplots = self._create_subplots(ranges)


    def _apply_compositor(self, holomap, ranges=None, keys=None, dimensions=None):
        """
        Given a HoloMap compute the appropriate (mapwise or framewise)
        ranges in order to apply the Compositor collapse operations in
        display mode (data collapse should already have happened).
        """
        # Compute framewise normalization
        defaultdim = holomap.ndims == 1 and holomap.kdims[0].name != 'Frame'

        if keys and ranges and dimensions and not defaultdim:
            dim_inds = [dimensions.index(d) for d in holomap.kdims]
            sliced_keys = [tuple(k[i] for i in dim_inds) for k in keys]
            frame_ranges = OrderedDict([(slckey, self.compute_ranges(holomap, key, ranges[key]))
                                        for key, slckey in zip(keys, sliced_keys) if slckey in holomap.data.keys()])
        else:
            mapwise_ranges = self.compute_ranges(holomap, None, None)
            frame_ranges = OrderedDict([(key, self.compute_ranges(holomap, key, mapwise_ranges))
                                        for key in holomap.keys()])
        ranges = frame_ranges.values()

        return Compositor.collapse(holomap, (ranges, frame_ranges.keys()), mode='display')


    def _create_subplots(self, ranges):
        subplots = OrderedDict()

        length = self.style_grouping
        ordering = util.layer_sort(self.map)
        keys, vmaps = self.map.split_overlays()
        group_fn = lambda x: (x.type.__name__, x.last.group, x.last.label)
        map_lengths = Counter()
        for m in vmaps:
            map_lengths[group_fn(m)[:length]] += 1

        zoffset = 0
        overlay_type = 1 if self.map.type == Overlay else 2
        group_counter = Counter()
        for (key, vmap) in zip(keys, vmaps):
            if self.map.type == Overlay:
                style_key = (vmap.type.__name__,) + key
            else:
                if not isinstance(key, tuple): key = (key,)
                style_key = group_fn(vmap) + key
            group_key = style_key[:length]
            zorder = ordering.index(style_key) + zoffset
            cyclic_index = group_counter[group_key]
            group_counter[group_key] += 1
            group_length = map_lengths[group_key]
            style = self.lookup_options(vmap.last, 'style').max_cycles(group_length)
            plotopts = dict(keys=self.keys, style=style, cyclic_index=cyclic_index,
                            zorder=self.zorder+zorder, ranges=ranges, overlaid=overlay_type,
                            layout_dimensions=self.layout_dimensions,
                            show_title=self.show_title, dimensions=self.dimensions,
                            uniform=self.uniform, show_legend=self.show_legend,
                            **{k: v for k, v in self.handles.items() if k in self._passed_handles})
            plotype = Store.registry[self.renderer.backend][type(vmap.last)]
            if not isinstance(key, tuple): key = (key,)
            subplots[key] = plotype(vmap, **plotopts)
            if issubclass(plotype, GenericOverlayPlot):
                zoffset += len(set([k for o in vmap for k in o.keys()])) - 1

        return subplots


    def _axis_labels(self, view, subplots, xlabel, ylabel, zlabel):
        return xlabel, ylabel, zlabel


    def get_extents(self, overlay, ranges):
        extents = []
        for key, subplot in self.subplots.items():
            layer = overlay.data.get(key, False)
            if layer and subplot.apply_ranges and not isinstance(layer, Annotation):
                if isinstance(layer, CompositeOverlay):
                    sp_ranges = ranges
                else:
                    sp_ranges = util.match_spec(layer, ranges) if ranges else {}
                extents.append(subplot.get_extents(layer, sp_ranges))
        return util.max_extents(extents, self.projection == '3d')


    def _format_title(self, key):
        frame = self._get_frame(key)
        if frame is None: return None

        type_name = type(frame).__name__
        group = frame.group if frame.group != type_name else ''
        label = frame.label
        if self.layout_dimensions:
            title = ''
        else:
            title_format = util.safe_unicode(self.title_format)
            title = title_format.format(label=util.safe_unicode(label),
                                        group=util.safe_unicode(group),
                                        type=type_name)
        dim_title = self._frame_title(key, 2)
        if not title or title.isspace():
            return dim_title
        elif not dim_title or dim_title.isspace():
            return title
        else:
            return '\n'.join([title, dim_title])



class GenericCompositePlot(DimensionedPlot):

    def _get_frame(self, key):
        """
        Creates a clone of the Layout with the nth-frame for each
        Element.
        """
        layout_frame = self.layout.clone(shared_data=False)
        nthkey_fn = lambda x: zip(tuple(x.name for x in x.kdims),
                                  list(x.data.keys())[min([key[0], len(x)-1])])
        for path, item in self.layout.items():
            if self.uniform:
                dim_keys = zip([d.name for d in self.dimensions
                                if d in item.dimensions('key')], key)
            else:
                dim_keys = item.traverse(nthkey_fn, (HoloMap,))[0]
            if dim_keys:
                obj = item.select((HoloMap,), **dict(dim_keys))
                if isinstance(obj, HoloMap) and len(obj) == 0:
                    continue
                else:
                    layout_frame[path] = obj
            else:
                layout_frame[path] = item
        return layout_frame


    def __len__(self):
        return len(self.keys)


    def _format_title(self, key):
        dim_title = self._frame_title(key, 3)
        layout = self.layout
        type_name = type(self.layout).__name__
        group = layout.group if layout.group != type_name else ''
        label = layout.label
        title = safe_unicode(self.title_format).format(label=safe_unicode(label),
                                                       group=safe_unicode(group),
                                                       type=type_name)
        title = '' if title.isspace() else title
        if not title:
            return dim_title
        elif not dim_title:
            return title
        else:
            return '\n'.join([title, dim_title])



class GenericLayoutPlot(GenericCompositePlot):
    """
    A GenericLayoutPlot accepts either a Layout or a NdLayout and
    displays the elements in a cartesian grid in scanline order.
    """

    def __init__(self, layout, **params):
        if not isinstance(layout, (NdLayout, Layout)):
            raise ValueError("GenericLayoutPlot only accepts Layout objects.")
        if len(layout.values()) == 0:
            raise ValueError("Cannot display empty layout")

        self.layout = layout
        self.subplots = {}
        self.rows, self.cols = layout.shape
        self.coords = list(product(range(self.rows),
                                   range(self.cols)))
        dimensions, keys = traversal.unique_dimkeys(layout)
        plotopts = self.lookup_options(layout, 'plot').options
        super(GenericLayoutPlot, self).__init__(keys=keys, dimensions=dimensions,
                                             uniform=traversal.uniform(layout),
                                             **dict(plotopts, **params))
