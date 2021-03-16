"""
Mapping routines to map to and from linear conductivities (what is used
internally) to other representations such as resistivities or logarithms
thereof.

Interpolation routines mapping values between different grids.
"""
# Copyright 2018-2021 The emg3d Developers.
#
# This file is part of emg3d.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy
# of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations under
# the License.

import numba as nb
import numpy as np
import scipy as sp
from scipy import ndimage as _      # noqa - sp.ndimage
from scipy import interpolate as _  # noqa - sp.interpolate

__all__ = ['MapConductivity', 'MapLgConductivity', 'MapLnConductivity',
           'MapResistivity', 'MapLgResistivity', 'MapLnResistivity',
           'interpolate', 'interp_spline_3d', 'interp_volume_average',
           'interp_edges_to_vol_averages']

# Numba-settings
_numba_setting = {'nogil': True, 'fastmath': True, 'cache': True}


# MAPS
MAPLIST = {}


def register_map(func):
    MAPLIST[func.__name__] = func
    return func


class BaseMap:
    """Maps variable `x` to computational variable `σ` (conductivity)."""

    def __init__(self, description):
        """Initiate the map."""
        self.name = self.__class__.__name__[3:]
        self.description = description

    def __repr__(self):
        return (f"{self.__class__.__name__}: {self.description}\n"
                "    Maps investigation variable `x` to\n"
                "    computational variable `σ` (conductivity).")

    def forward(self, conductivity):
        """Conductivity to mapping."""
        raise NotImplementedError("Forward map not implemented.")

    def backward(self, mapped):
        """Mapping to conductivity."""
        raise NotImplementedError("Backward map not implemented.")

    def derivative_chain(self, gradient, mapped):
        """Chain rule to map gradient from conductivity to mapping space."""
        raise NotImplementedError("Derivative chain not implemented.")

    def to_dict(self):
        """Store the map name in a dict for serialization."""
        return {'name': self.name, '__class__': 'BaseMap'}

    @classmethod
    def from_dict(cls, inp):
        """Get :class:`BaseMap` instance from name in dict."""
        return MAPLIST['Map'+inp['name']]()


@register_map
class MapConductivity(BaseMap):
    """Maps `σ` to computational variable `σ` (conductivity).

    - forward: x = σ
    - backward: σ = x

    """

    def __init__(self):
        super().__init__('conductivity')

    def forward(self, conductivity):
        return conductivity

    def backward(self, mapped):
        return mapped

    def derivative_chain(self, gradient, mapped):
        pass


@register_map
class MapLgConductivity(BaseMap):
    """Maps `log_10(σ)` to computational variable `σ` (conductivity).

    - forward: x = log_10(σ)
    - backward: σ = 10^x

    """

    def __init__(self):
        super().__init__('log_10(conductivity)')

    def forward(self, conductivity):
        return np.log10(conductivity)

    def backward(self, mapped):
        return 10**mapped

    def derivative_chain(self, gradient, mapped):
        gradient *= self.backward(mapped)*np.log(10)


@register_map
class MapLnConductivity(BaseMap):
    """Maps `log_e(σ)` to computational variable `σ` (conductivity).

    - forward: x = log_e(σ)
    - backward: σ = exp(x)

    """

    def __init__(self):
        super().__init__('log_e(conductivity)')

    def forward(self, conductivity):
        return np.log(conductivity)

    def backward(self, mapped):
        return np.exp(mapped)

    def derivative_chain(self, gradient, mapped):
        gradient *= self.backward(mapped)


@register_map
class MapResistivity(BaseMap):
    """Maps `ρ` to computational variable `σ` (conductivity).

    - forward: x = ρ = σ^-1
    - backward: σ = ρ^-1 = x^-1

    """

    def __init__(self):
        super().__init__('resistivity')

    def forward(self, conductivity):
        return 1.0/conductivity

    def backward(self, mapped):
        return 1.0/mapped

    def derivative_chain(self, gradient, mapped):
        gradient *= -self.backward(mapped)**2


@register_map
class MapLgResistivity(BaseMap):
    """Maps `log_10(ρ)` to computational variable `σ` (conductivity).

    - forward: x = log_10(ρ) = log_10(σ^-1)
    - backward: σ = ρ^-1 = 10^-x

    """

    def __init__(self):
        super().__init__('log_10(resistivity)')

    def forward(self, conductivity):
        return np.log10(1.0/conductivity)

    def backward(self, mapped):
        return 10**-mapped

    def derivative_chain(self, gradient, mapped):
        gradient *= -self.backward(mapped)*np.log(10)


@register_map
class MapLnResistivity(BaseMap):
    """Maps `log_e(ρ)` to computational variable `σ` (conductivity).

    - forward: x = log_e(ρ) = log_e(σ^-1)
    - backward: σ = ρ^-1 = exp(-x)

    """

    def __init__(self):
        super().__init__('log_e(resistivity)')

    def forward(self, conductivity):
        return np.log(1.0/conductivity)

    def backward(self, mapped):
        return np.exp(-mapped)

    def derivative_chain(self, gradient, mapped):
        gradient *= -self.backward(mapped)


# INTERPOLATIONS
def interpolate(grid, values, xi, method='linear', extrapolate=True,
                log=False, **kwargs):
    """Interpolate values from one grid to another grid or points.


    Parameters
    ----------
    grid : TensorMesh
        Input grid; a :class:`emg3d.meshes.TensorMesh` instance.

    values : ndarray
        A model property such as ``Model.property_x``, or a particular field
        such as ``Field.fx`` (``ndim=3``). The dimensions in each directions
        must either correspond to the number of nodes or edges in the
        corresponding direction.

    xi : {ndarray, TensorMesh}
        Output coordinates:

        - A grid (:class:`emg3d.meshes.TensorMesh`): interpolation from one
          grid to another.
        - Arbitrary point coordinates as ``ndarray`` of shape ``(..., 3)``:
          returns a flat array with the values on the provided coordinates.

    method : {'nearest', 'linear', 'volume', 'cubic'}, default: ``'linear'``
        The method of interpolation to perform.

        - ``'nearest', 'linear'``: Fastest methods; work for model properties
          and fields living on edges;
          :class:`scipy.interpolateu.RegularGridInterpolator`.

        - ``'cubic'``: Requires at least four points in any direction;
          :func:`emg3d.maps.interp_spline_3d`.

        - ``'volume'``: Ensures that the total sum of the interpolated quantity
          stays constant; :func:`emg3d.maps.interp_volume_average`.

          The result can be quite different if you provide resistivity,
          conductivity, or the logarithm of any of the two. The recommended way
          is to provide the logarithm of resistivity or conductivity, in which
          case the output of one is indeed the inverse of the output of the
          other.

          This method is only implemented for quantities living on cell
          centers, not on edges (hence not for fields); and only for grids as
          input to ``xi``.

    extrapolate : bool, default: ``True``
        This parameter controls the default parameters provided to the
        interpolation routines.

        - ``'nearest', 'linear'``: If True, values outside of the domain are
          extrapolated (``bounds_error=False, fill_value=None``); if False,
          values outside are set to 0.0 (``bounds_error=False,
          fill_value=0.0``)

        - ``'cubic'``: If True, values outside of the domain are extrapolated
          using nearest interpolation (``mode='nearest'``); if False, values
          outside are set to (``mode='constant'``)

        - ``'volume'``: Always uses nearest interpolation for points outside of
          the provided grid, independent of the choice of ``extrapolate``.

    log : bool, default: ``False``
        If True, the interpolation is carried out on a log10-scale; corresponds
        to ``10**interpolate(grid, np.log10(values), ...)``.

    kwargs : dict, optional
        Will be forwarded to the corresponding interpolation algorithm, if they
        accept additional keywords.


    Returns
    -------
    values_x : ndarray
        Values corresponding to the new grid.

    """

    # # Input checks # #

    # Check if 'xi' is an ndarray; else assume it is a TensorMesh.
    xi_is_grid = not isinstance(xi, (np.ndarray, tuple))

    # The values must either live on cell centers or on edges.
    if np.ndim(values) != 3:
        msg = ("``values`` must be a 3D ndarray living on cell centers or "
               "edges of the ``grid``.")
        raise ValueError(msg)

    # For 'volume', the shape of the values must correspond to shape of cells.
    if method == 'volume' and not np.all(grid.shape_cells == values.shape):
        raise ValueError("``method='volume'`` not implemented for fields.")

    # For 'volume' 'xi' must be a TensorMesh.
    if method == 'volume' and not xi_is_grid:
        msg = ("``method='volume'`` only implemented for TensorMesh "
               "instances as input for ``xi``.")
        raise ValueError(msg)

    # Check enough points for cubic (req. would be order+1; default order=3).
    if method == 'cubic' and any([x < 4 for x in values.shape]):
        msg = ("``method='cubic'`` needs at least four points in each "
               "dimension.")
        raise ValueError(msg)

    # # Take log10 if set # #
    if log:
        values = np.log10(values)

    # # Get points from input grids # #

    # Initiate points and new_points, if required.
    points = tuple()
    if xi_is_grid:
        new_points = tuple()
        shape = tuple()

    # Loop over dimensions to get the vectors corresponding to input data.
    for i, coord in enumerate(['x', 'y', 'z']):

        # Cell nodes.
        if method == 'volume' or values.shape[i] == grid.shape_nodes[i]:
            pts = getattr(grid, 'nodes_'+coord)
            if xi_is_grid:
                new_pts = getattr(xi, 'nodes_'+coord)

        # Cell centers.
        else:
            pts = getattr(grid, 'cell_centers_'+coord)
            if xi_is_grid:
                new_pts = getattr(xi, 'cell_centers_'+coord)

        # Add to points.
        points += (pts, )
        if xi_is_grid:
            new_points += (new_pts, )
            shape += (len(new_pts), )

    # # Use `interp_volume_average` if method is 'volume' # #
    if method == 'volume':
        values_x = np.zeros(xi.shape_cells, order='F', dtype=values.dtype)
        vol = xi.cell_volumes.reshape(xi.shape_cells, order='F')
        interp_volume_average(
                *points, values, *new_points, values_x, new_vol=vol)

    # Coordinates/shape are different handled for volume than the rest.
    else:

        # # Convert points to correct format # #

        if xi_is_grid:
            xx, yy, zz = np.broadcast_arrays(
                    new_points[0][:, None, None],
                    new_points[1][:, None],
                    new_points[2])
            new_points = np.r_[xx.ravel('F'), yy.ravel('F'), zz.ravel('F')]
            new_points = new_points.reshape(-1, 3, order='F')

        else:
            # Replicate the same expansion of xi as used in
            # RegularGridInterpolator, so the input xi can be quite flexible.
            new_points = sp.interpolate.interpnd._ndim_coords_from_arrays(
                    xi, ndim=3)
            shape = new_points.shape[:-1]
            new_points = new_points.reshape(-1, 3)

        # # Use `interp_spline_3d` if method is 'cubic' # #
        if method == 'cubic':

            map_opts = {
                'mode': 'nearest' if extrapolate else 'constant',
                **({} if kwargs is None else kwargs),
            }

            values_x = interp_spline_3d(
                    points, values, new_points,
                    map_opts=map_opts)

        # # Use `RegularGridInterpolator` if method is 'nearest'/'linear' # #
        else:

            opts = {
                'bounds_error': False,
                'fill_value': None if extrapolate else 0.0,
                **({} if kwargs is None else kwargs),
            }

            values_x = sp.interpolate.RegularGridInterpolator(
                    points=points, values=values, method=method,
                    **opts)(xi=new_points)

        # # Reshape accordingly # #
        values_x = values_x.reshape(shape, order='F')

    # # Come back if we were on log10. # #
    if log:
        values_x = 10**values_x

    return values_x


def interp_spline_3d(points, values, xi, map_opts=None, interp1d_opts=None):
    """Interpolate values in 3D with a cubic spline.

    This functionality is best accessed through :func:`emg3d.maps.interpolate`
    by setting ``method='cubic'``.

    This custom version of :func:`scipy.ndimage.map_coordinates` enables 3D
    cubic interpolation. This is achieved by a cubic interpolation of the new
    points from the old points using :func:`scipy.interpolate.interp1d` for
    each direction to bring the new points onto the artificial index coordinate
    system of ndimage. Once we have the coordinates we can call
    :func:`scipy.ndimage.map_coordinates`


    Parameters
    ----------
    points : (ndarray, ndarray, ndarray)
        The points defining the regular grid in (x, y, z) direction.

    values : ndarray
        The data on the regular grid in three dimensions (nx, ny, nz).

    xi : ndarray
        Coordinates (x, y, z) of new points, shape ``(..., 3)``.

    map_opts : dict, default: None
        Passed through to :func:`scipy.ndimage.map_coordinates`.

    interp1d_opts : dict, default: None
        Passed through to :func:`scipy.interpolate.interp1d`.

        The default behaviour of ``interp_cube_3d`` is to pass
        ``kind='cubic'``, ``bounds_error=False``, and ``fill_value=
        'extrapolate'``)


    Returns
    -------
    values_x : ndarray
        Values corresponding to ``xi``.

    """

    # `map_coordinates` uses the indices of the input data (our values) as
    # coordinates. We have therefore to transform our desired output
    # coordinates to this artificial coordinate system too.
    interp1d_opts = {
        'kind': 'cubic',
        'bounds_error': False,
        'fill_value': 'extrapolate',
        **({} if interp1d_opts is None else interp1d_opts),
    }
    coords = np.empty(xi.T.shape)
    for i in range(3):
        coords[i] = sp.interpolate.interp1d(
                points[i], np.arange(len(points[i])),
                **interp1d_opts)(xi[:, i])

    # `map_coordinates` only works for real data; split it up if complex.
    # Note: SciPy 1.6 (12/2020) introduced complex-valued
    #       ndimage.map_coordinates; replace eventually.
    map_opts = ({} if map_opts is None else map_opts)
    values_x = sp.ndimage.map_coordinates(values.real, coords, **map_opts)
    if 'complex' in values.dtype.name:
        imag = sp.ndimage.map_coordinates(values.imag, coords, **map_opts)
        values_x = values_x + 1j*imag

    return values_x


@nb.njit(**_numba_setting)
def interp_volume_average(
        edges_x, edges_y, edges_z, values, new_edges_x, new_edges_y,
        new_edges_z, new_values, new_vol):
    """Interpolate values defined on cell centers to volume-averaged values.

    The ``field`` is assumed to be from a :class:`emg3d.fields.Field` instance.

    This functionality is best accessed through :func:`emg3d.maps.interpolate`
    by setting ``method='volume'``.

    Interpolation using the volume averaging technique. The original
    implementation (see ``emg3d v0.7.1``) followed [PlDM07]_. Joseph Capriotti
    took that algorithm and made it much faster for implementation in
    *discretize*. The current implementation is a simplified version of his
    (the *discretize* version works for 1D, 2D, and 3D meshes and can also
    return a sparse matrix representing the operation), translated from Cython
    to Numba.

    The result is added to ``new_values``.


    Parameters
    ----------
    edges_{x;y;z} : ndarray
        The edges in x-, y-, and z-directions for the original grid.

    values : ndarray
        Values corresponding to original grid.

    new_edges_{x;y;z} : ndarray
        The edges in x-, y-, and z-directions for the new grid.

    new_values : ndarray
        Array where values corresponding to the new grid will be added.

    new_vol : ndarray
        The cell volumes of the new grid.

    """

    # Get the weights and indices for each direction.
    wx, ix_in, ix_out = _volume_average_weights(edges_x, new_edges_x)
    wy, iy_in, iy_out = _volume_average_weights(edges_y, new_edges_y)
    wz, iz_in, iz_out = _volume_average_weights(edges_z, new_edges_z)

    # Loop over the elements and sum up the contributions.
    for iz, w_z in enumerate(wz):
        izi = iz_in[iz]
        izo = iz_out[iz]
        for iy, w_y in enumerate(wy):
            iyi = iy_in[iy]
            iyo = iy_out[iy]
            w_zy = w_z*w_y
            for ix, w_x in enumerate(wx):
                ixi = ix_in[ix]
                ixo = ix_out[ix]
                new_values[ixo, iyo, izo] += w_zy*w_x*values[ixi, iyi, izi]

    # Normalize by new volume.
    new_values /= new_vol


@nb.njit(**_numba_setting)
def _volume_average_weights(x1, x2):
    """Return the weights for the volume averaging technique.


    Parameters
    ----------
    x1, x2 : ndarray
        The edges in x-, y-, or z-directions for the original (x1) and the new
        (x2) grids.


    Returns
    -------
    hs : ndarray
        Weights for the mapping of x1 to x2.

    ix1, ix2 : ndarray
        Indices to map x1 to x2.

    """
    # Get unique edges.
    xs = np.unique(np.concatenate((x1, x2)))
    n1, n2, nh = len(x1), len(x2), len(xs)-1

    # Get weights and indices for the two arrays.
    # - hs corresponds to np.diff(xs) where x1 and x2 overlap; zero outside.
    # - x1[ix1] can be mapped to x2[ix2] with the corresponding weight.
    hs = np.empty(nh)                   # Pre-allocate weights.
    ix1 = np.zeros(nh, dtype=np.int32)  # Pre-allocate indices for x1.
    ix2 = np.zeros(nh, dtype=np.int32)  # Pre-allocate indices for x2.
    center = 0.0
    i1, i2, i, ii = 0, 0, 0, 0
    for i in range(nh):
        center = 0.5*(xs[i]+xs[i+1])
        if x2[0] <= center and center <= x2[n2-1]:
            hs[ii] = xs[i+1]-xs[i]
            while i1 < n1-1 and center >= x1[i1]:
                i1 += 1
            while i2 < n2-1 and center >= x2[i2]:
                i2 += 1
            ix1[ii] = min(max(i1-1, 0), n1-1)
            ix2[ii] = min(max(i2-1, 0), n2-1)
            ii += 1

    return hs[:ii], ix1[:ii], ix2[:ii]


@nb.njit(**_numba_setting)
def interp_edges_to_vol_averages(ex, ey, ez, volumes, ox, oy, oz):
    r"""Interpolate fields defined on edges to volume-averaged cell values.

    The ``field`` is assumed to be from a :class:`emg3d.fields.Field` instance.

    Parameters
    ----------
    ex, ey, ez : ndarray
        Electric fields in x-, y-, and z-directions (``field.f{x;y;z}``).

    volumes : ndarray
        Cell volumes of the grid (``field.grid.cell_volumes``).

    ox, oy, oz : ndarray
        Output arrays (of shape ``field.grid.shape_cells``) where the results
        are placed (per direction).

    """

    # Get dimensions
    nx, ny, nz = volumes.shape

    # Loop over dimensions.
    for iz in range(nz+1):
        izm = max(0, iz-1)
        izp = min(nz-1, iz)

        for iy in range(ny+1):
            iym = max(0, iy-1)
            iyp = min(ny-1, iy)

            for ix in range(nx+1):
                ixm = max(0, ix-1)
                ixp = min(nx-1, ix)

                # Multiply field by volume/4.
                if ix < nx:
                    ox[ix, iym, izm] += volumes[ix, iym, izm]*ex[ix, iy, iz]/4
                    ox[ix, iyp, izm] += volumes[ix, iyp, izm]*ex[ix, iy, iz]/4
                    ox[ix, iym, izp] += volumes[ix, iym, izp]*ex[ix, iy, iz]/4
                    ox[ix, iyp, izp] += volumes[ix, iyp, izp]*ex[ix, iy, iz]/4

                if iy < ny:
                    oy[ixm, iy, izm] += volumes[ixm, iy, izm]*ey[ix, iy, iz]/4
                    oy[ixp, iy, izm] += volumes[ixp, iy, izm]*ey[ix, iy, iz]/4
                    oy[ixm, iy, izp] += volumes[ixm, iy, izp]*ey[ix, iy, iz]/4
                    oy[ixp, iy, izp] += volumes[ixp, iy, izp]*ey[ix, iy, iz]/4

                if iz < nz:
                    oz[ixm, iym, iz] += volumes[ixm, iym, iz]*ez[ix, iy, iz]/4
                    oz[ixp, iym, iz] += volumes[ixp, iym, iz]*ez[ix, iy, iz]/4
                    oz[ixm, iyp, iz] += volumes[ixm, iyp, iz]*ez[ix, iy, iz]/4
                    oz[ixp, iyp, iz] += volumes[ixp, iyp, iz]*ez[ix, iy, iz]/4
