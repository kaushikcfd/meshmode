__copyright__ = "Copyright (C) 2014 Andreas Kloeckner"

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""

import numpy as np
from pytools import memoize_method, Record
from meshmode.dof_array import DOFArray


__doc__ = """

.. autofunction:: make_visualizer

.. autoclass:: Visualizer

.. autofunction:: write_nodal_adjacency_vtk_file
"""


# {{{ visualizer

def separate_by_real_and_imag(data, real_only):
    # This function is called on numpy data that has already been
    # merged into a single vector.

    for name, field in data:
        if isinstance(field, np.ndarray) and field.dtype.char == "O":
            assert len(field.shape) == 1
            from pytools.obj_array import (
                    obj_array_real_copy, obj_array_imag_copy,
                    obj_array_vectorize)

            if field[0].dtype.kind == "c":
                if real_only:
                    yield (name,
                            obj_array_vectorize(obj_array_real_copy, field))
                else:
                    yield (name+"_r",
                            obj_array_vectorize(obj_array_real_copy, field))
                    yield (name+"_i",
                            obj_array_vectorize(obj_array_imag_copy, field))
            else:
                yield (name, field)
        else:
            if field.dtype.kind == "c":
                yield (name+"_r", field.real.copy())
                yield (name+"_i", field.imag.copy())
            else:
                yield (name, field)


class _VisConnectivityGroup(Record):
    """
    .. attribute:: vis_connectivity

        an array of shape ``(group.nelements,nsubelements,primitive_element_size)``

    .. attribute:: vtk_cell_type

    .. attribute:: subelement_nr_base
    """

    @property
    def nsubelements(self):
        return self.nelements * self.nsubelements_per_element

    @property
    def nelements(self):
        return self.vis_connectivity.shape[0]

    @property
    def nsubelements_per_element(self):
        return self.vis_connectivity.shape[1]

    @property
    def primitive_element_size(self):
        return self.vis_connectivity.shape[2]


class Visualizer(object):
    """
    .. automethod:: show_scalar_in_mayavi
    .. automethod:: show_scalar_in_matplotlib_3d
    .. automethod:: write_vtk_file
    """

    def __init__(self, connection, element_shrink_factor=None):
        self.connection = connection
        self.discr = connection.from_discr
        self.vis_discr = connection.to_discr

        if element_shrink_factor is None:
            element_shrink_factor = 1

        self.element_shrink_factor = element_shrink_factor

    def _resample_to_numpy(self, vec):
        if (isinstance(vec, np.ndarray)
                and vec.dtype.char == "O"
                and not isinstance(vec, DOFArray)):
            from pytools.obj_array import obj_array_vectorize
            return obj_array_vectorize(self._resample_to_numpy, vec)

        from numbers import Number
        if isinstance(vec, Number):
            raise NotImplementedError("visualizing constants")
            #return np.ones(self.connection.to_discr.nnodes) * fld
        else:
            resampled = self.connection(vec)
            if len(resampled) != 1:
                raise NotImplementedError("visualization with multiple "
                        "element groups")
            return resampled.array_context.to_numpy(resampled[0]).reshape(-1)

    @memoize_method
    def _vis_nodes(self):
        if len(self.vis_discr.groups) != 1:
            raise NotImplementedError("visualization with multiple "
                    "element groups")

        actx = self.vis_discr._setup_actx
        return np.array([
            actx.to_numpy(actx.thaw(ary[0]))
            for ary in self.vis_discr.nodes()
            ])

    # {{{ vis sub-element connectivity

    @memoize_method
    def _vis_connectivity(self):
        """
        :return: a list of :class:`_VisConnectivityGroup` instances.
        """
        # Assume that we're using modepy's default node ordering.

        from pytools import (
                generate_nonnegative_integer_tuples_summing_to_at_most as gnitstam,
                generate_nonnegative_integer_tuples_below as gnitb)
        from meshmode.mesh import TensorProductElementGroup, SimplexElementGroup

        result = []

        from pyvisfile.vtk import (
                VTK_LINE, VTK_TRIANGLE, VTK_TETRA,
                VTK_QUAD, VTK_HEXAHEDRON)

        subel_nr_base = 0
        node_nr_base = 0

        for group in self.vis_discr.groups:
            if isinstance(group.mesh_el_group, SimplexElementGroup):
                node_tuples = list(gnitstam(group.order, group.dim))

                from modepy.tools import simplex_submesh
                el_connectivity = np.array(
                        simplex_submesh(node_tuples),
                        dtype=np.intp)
                vtk_cell_type = {
                        1: VTK_LINE,
                        2: VTK_TRIANGLE,
                        3: VTK_TETRA,
                        }[group.dim]

            elif isinstance(group.mesh_el_group, TensorProductElementGroup):
                node_tuples = list(gnitb(group.order+1, group.dim))
                node_tuple_to_index = dict(
                        (nt, i) for i, nt in enumerate(node_tuples))

                def add_tuple(a, b):
                    return tuple(ai+bi for ai, bi in zip(a, b))

                el_offsets = {
                        1: [(0,), (1,)],
                        2: [(0, 0), (1, 0), (1, 1), (0, 1)],
                        3: [
                            (0, 0, 0),
                            (1, 0, 0),
                            (1, 1, 0),
                            (0, 1, 0),
                            (0, 0, 1),
                            (1, 0, 1),
                            (1, 1, 1),
                            (0, 1, 1),
                            ]
                        }[group.dim]

                el_connectivity = np.array([
                        [
                            node_tuple_to_index[add_tuple(origin, offset)]
                            for offset in el_offsets]
                        for origin in gnitb(group.order, group.dim)])

                vtk_cell_type = {
                        1: VTK_LINE,
                        2: VTK_QUAD,
                        3: VTK_HEXAHEDRON,
                        }[group.dim]

            else:
                raise NotImplementedError("visualization for element groups "
                        "of type '%s'" % type(group.mesh_el_group).__name__)

            assert len(node_tuples) == group.nunit_dofs
            vis_connectivity = (
                    node_nr_base + np.arange(
                        0, group.nelements*group.nunit_dofs, group.nunit_dofs
                        )[:, np.newaxis, np.newaxis]
                    + el_connectivity).astype(np.intp)

            vgrp = _VisConnectivityGroup(
                vis_connectivity=vis_connectivity,
                vtk_cell_type=vtk_cell_type,
                subelement_nr_base=subel_nr_base)
            result.append(vgrp)

            subel_nr_base += vgrp.nsubelements
            node_nr_base += group.ndofs

        return result

    # }}}

    # {{{ mayavi

    def show_scalar_in_mayavi(self, field, **kwargs):
        import mayavi.mlab as mlab

        do_show = kwargs.pop("do_show", True)

        nodes = self._vis_nodes()
        field = self._resample_to_numpy(field)

        assert nodes.shape[0] == self.vis_discr.ambient_dim
        #mlab.points3d(nodes[0], nodes[1], 0*nodes[0])

        vis_connectivity, = self._vis_connectivity()

        if self.vis_discr.dim == 1:
            nodes = list(nodes)
            # pad to 3D with zeros
            while len(nodes) < 3:
                nodes.append(0*nodes[0])
            assert len(nodes) == 3

            args = tuple(nodes) + (field,)

            # http://docs.enthought.com/mayavi/mayavi/auto/example_plotting_many_lines.html  # noqa
            src = mlab.pipeline.scalar_scatter(*args)

            src.mlab_source.dataset.lines = vis_connectivity.reshape(-1, 2)
            lines = mlab.pipeline.stripper(src)
            mlab.pipeline.surface(lines, **kwargs)

        elif self.vis_discr.dim == 2:
            nodes = list(nodes)
            # pad to 3D with zeros
            while len(nodes) < 3:
                nodes.append(0*nodes[0])

            args = tuple(nodes) + (vis_connectivity.reshape(-1, 3),)
            kwargs["scalars"] = field

            mlab.triangular_mesh(*args, **kwargs)

        else:
            raise RuntimeError("meshes of bulk dimension %d are currently "
                    "unsupported" % self.vis_discr.dim)

        if do_show:
            mlab.show()

    # }}}

    # {{{ vtk

    def write_vtk_file(self, file_name, names_and_fields,
                       compressor=None,
                       real_only=False,
                       overwrite=False):

        from pyvisfile.vtk import (
                UnstructuredGrid, DataArray,
                AppendedDataXMLGenerator,
                VF_LIST_OF_COMPONENTS)

        nodes = self._vis_nodes()
        names_and_fields = [
                (name, self._resample_to_numpy(fld))
                for name, fld in names_and_fields]

        vc_groups = self._vis_connectivity()

        # {{{ create cell_types

        nsubelements = sum(vgrp.nsubelements for vgrp in vc_groups)
        cell_types = np.empty(nsubelements, dtype=np.uint8)
        cell_types.fill(255)
        for vgrp in vc_groups:
            cell_types[
                    vgrp.subelement_nr_base:
                    vgrp.subelement_nr_base + vgrp.nsubelements] = \
                            vgrp.vtk_cell_type
        assert (cell_types < 255).all()

        # }}}

        if self.element_shrink_factor != 1:
            for vgrp in self.vis_discr.groups:
                nodes_view = vgrp.view(nodes)
                el_centers = np.mean(nodes_view, axis=-1)
                nodes_view[:] = (
                        (self.element_shrink_factor * nodes_view)
                        + (1-self.element_shrink_factor)
                        * el_centers[:, :, np.newaxis])

        if len(self.vis_discr.groups) != 1:
            raise NotImplementedError("visualization with multiple "
                    "element groups")

        grid = UnstructuredGrid(
                (self.vis_discr.groups[0].ndofs,
                    DataArray("points",
                        nodes.reshape(self.vis_discr.ambient_dim, -1),
                        vector_format=VF_LIST_OF_COMPONENTS)),
                cells=np.hstack([
                    vgrp.vis_connectivity.reshape(-1)
                    for vgrp in vc_groups]),
                cell_types=cell_types)

        # for name, field in separate_by_real_and_imag(cell_data, real_only):
        #     grid.add_celldata(DataArray(name, field,
        #         vector_format=VF_LIST_OF_COMPONENTS))

        for name, field in separate_by_real_and_imag(names_and_fields, real_only):
            grid.add_pointdata(DataArray(name, field,
                vector_format=VF_LIST_OF_COMPONENTS))

        import os
        from meshmode import FileExistsError
        if os.path.exists(file_name):
            if overwrite:
                os.remove(file_name)
            else:
                raise FileExistsError("output file '%s' already exists" % file_name)

        with open(file_name, "w") as outf:
            AppendedDataXMLGenerator(compressor)(grid).write(outf)

        # }}}

    # {{{ matplotlib 3D

    def show_scalar_in_matplotlib_3d(self, field, **kwargs):
        import matplotlib.pyplot as plt

        # This import also registers the 3D projection.
        import mpl_toolkits.mplot3d.art3d as art3d

        do_show = kwargs.pop("do_show", True)
        vmin = kwargs.pop("vmin", None)
        vmax = kwargs.pop("vmax", None)
        norm = kwargs.pop("norm", None)

        nodes = self._vis_nodes()
        field = self._resample_to_numpy(field)

        assert nodes.shape[0] == self.vis_discr.ambient_dim

        vis_connectivity, = self._vis_connectivity()

        fig = plt.gcf()
        ax = fig.gca(projection="3d")

        had_data = ax.has_data()

        if self.vis_discr.dim == 2:
            nodes = list(nodes)
            # pad to 3D with zeros
            while len(nodes) < 3:
                nodes.append(0*nodes[0])

            from matplotlib.tri.triangulation import Triangulation
            tri, args, kwargs = \
                Triangulation.get_from_args_and_kwargs(
                        *nodes,
                        triangles=vis_connectivity.vis_connectivity.reshape(-1, 3))

            triangles = tri.get_masked_triangles()
            xt = nodes[0][triangles]
            yt = nodes[1][triangles]
            zt = nodes[2][triangles]
            verts = np.stack((xt, yt, zt), axis=-1)

            fieldt = field[triangles]

            polyc = art3d.Poly3DCollection(verts, **kwargs)

            # average over the three points of each triangle
            avg_field = fieldt.mean(axis=1)
            polyc.set_array(avg_field)

            if vmin is not None or vmax is not None:
                polyc.set_clim(vmin, vmax)
            if norm is not None:
                polyc.set_norm(norm)

            ax.add_collection(polyc)
            ax.auto_scale_xyz(xt, yt, zt, had_data)

        else:
            raise RuntimeError("meshes of bulk dimension %d are currently "
                    "unsupported" % self.vis_discr.dim)

        if do_show:
            plt.show()

    # }}}


def make_visualizer(actx, discr, vis_order, element_shrink_factor=None):
    from meshmode.discretization import Discretization
    from meshmode.discretization.poly_element import (
            PolynomialWarpAndBlendElementGroup,
            LegendreGaussLobattoTensorProductElementGroup,
            OrderAndTypeBasedGroupFactory)
    vis_discr = Discretization(
            actx, discr.mesh,
            OrderAndTypeBasedGroupFactory(
                vis_order,
                simplex_group_class=PolynomialWarpAndBlendElementGroup,
                tensor_product_group_class=(
                    LegendreGaussLobattoTensorProductElementGroup)),
            real_dtype=discr.real_dtype)
    from meshmode.discretization.connection import \
            make_same_mesh_connection

    return Visualizer(
            make_same_mesh_connection(actx, vis_discr, discr),
            element_shrink_factor=element_shrink_factor)

# }}}


# {{{ draw_curve

def draw_curve(discr):
    mesh = discr.mesh

    import matplotlib.pyplot as plt
    plt.plot(mesh.vertices[0], mesh.vertices[1], "o")

    color = plt.cm.rainbow(np.linspace(0, 1, len(discr.groups)))
    for igrp, group in enumerate(discr.groups):
        group_nodes = np.array([
            discr._setup_actx.to_numpy(discr.nodes()[iaxis][igrp])
            for iaxis in range(discr.ambient_dim)
            ])
        artist_handles = plt.plot(
                group_nodes[0].T,
                group_nodes[1].T, "-x",
                color=color[igrp])

        if artist_handles:
            artist_handles[0].set_label("Group %d" % igrp)

# }}}


# {{{ adjacency

def write_nodal_adjacency_vtk_file(file_name, mesh,
                                   compressor=None,
                                   overwrite=False):
    from pyvisfile.vtk import (
            UnstructuredGrid, DataArray,
            AppendedDataXMLGenerator,
            VTK_LINE,
            VF_LIST_OF_COMPONENTS)

    centroids = np.empty(
            (mesh.ambient_dim, mesh.nelements),
            dtype=mesh.vertices.dtype)

    for grp in mesh.groups:
        iel_base = grp.element_nr_base
        centroids[:, iel_base:iel_base+grp.nelements] = (
                np.sum(mesh.vertices[:, grp.vertex_indices], axis=-1)
                / grp.vertex_indices.shape[-1])

    adj = mesh.nodal_adjacency

    nconnections = len(adj.neighbors)
    connections = np.empty((nconnections, 2), dtype=np.int32)

    nb_starts = adj.neighbors_starts
    for iel in range(mesh.nelements):
        connections[nb_starts[iel]:nb_starts[iel+1], 0] = iel

    connections[:, 1] = adj.neighbors

    grid = UnstructuredGrid(
            (mesh.nelements,
                DataArray("points",
                    centroids,
                    vector_format=VF_LIST_OF_COMPONENTS)),
            cells=connections.reshape(-1),
            cell_types=np.asarray([VTK_LINE] * nconnections,
                dtype=np.uint8))

    import os
    from meshmode import FileExistsError
    if os.path.exists(file_name):
        if overwrite:
            os.remove(file_name)
        else:
            raise FileExistsError("output file '%s' already exists" % file_name)

    with open(file_name, "w") as outf:
        AppendedDataXMLGenerator(compressor)(grid).write(outf)

# }}}

# vim: foldmethod=marker
