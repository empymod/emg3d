"""
A simulation is the computation (modelling) of electromagnetic responses of a
resistivity (conductivity) model for a given survey.

In its heart, `emg3d` is a multigrid solver for 3D electromagnetic diffusion
with tri-axial electrical anisotropy. However, it contains most functionalities
to also act as a modeller. The simulation module combines all these things
by combining surveys with computational meshes and fields and providing
high-level, specialised modelling routines.
"""
# Copyright 2018-2020 The emg3d Developers.
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

import itertools
from copy import deepcopy

import numpy as np
import scipy.linalg as sl

try:
    from tqdm.contrib.concurrent import process_map
except ImportError:
    # If you have tqdm installed, but don't want to use it, simply do
    # `emg3d.simulation.process_map = emg3d.utils._process_map`.
    from emg3d.utils import _process_map as process_map

from emg3d import fields, solver, surveys, maps, models, meshes, optimize

__all__ = ['Simulation', 'expand_grid_model', 'estimate_gridding_opts']


class Simulation:
    """Create a simulation for a given survey on a given model.

    A simulation can be used to compute responses for an entire survey, hence
    for an arbitrary amount of sources, receivers, and frequencies. The
    responses can be computed in parallel over sources and frequencies. It can
    also be used to compute the misfit with the data and to compute the
    gradient of the misfit function.

    The computational grid(s) can either be provided, or automatic gridding can
    be used; see the description of the parameters ``gridding`` and
    ``gridding_opts`` for more details.

    .. note::

        The Simulation-class has currently a few limitations:

        - `survey.fixed`: must be `False`;
        - sources and receivers must be electric;


    Parameters
    ----------
    survey : :class:`emg3d.surveys.Survey`
        The survey layout, containing sources, receivers, frequencies, and
        optionally the measured data.

        The survey-data will be modified in place. Provide survey.copy() if you
        want to avoid this.

    grid : :class:`emg3d.meshes.TensorMesh`
        The grid. See :class:`emg3d.meshes.TensorMesh`.

    model : :class:`emg3d.models.Model`
        The model. See :class:`emg3d.models.Model`.

    gridding : str, optional
        Method how the computational grids are computed. Default is 'single'.
        The different methods are:

        - 'same': Same grid as for the input model.
        - 'single': A single grid for all sources and frequencies.
        - 'frequency': Frequency-dependent grids.
        - 'source': Source-dependent grids.
        - 'both': Frequency- and source-dependent grids.
        - 'input': Same as 'single', but instead of automatically generate
          the mesh it has to be provided ``gridding_opts``.
        - 'dict': Same as 'both', but instead of automatically generate the
          meshes they have to be provided as a ``dict[source][frequency]``
          in ``gridding_opts``.

        See the parameter ``gridding_opts`` for more details.

    gridding_opts : dict or TensorMesh, optional
        Input format depends on ``gridding``:

        - 'same': Nothing, ``gridding_opts`` is not permitted.
        - 'single', 'frequency', 'source', 'both': Described below.
        - 'input': A :class:`emg3d.meshes.TensorMesh` mesh.
        - 'dict': Dictionary of the format ``dict[source][frequency]``
          containing a :class:`emg3d.meshes.TensorMesh` mesh for each
          source-frequency pair.

        The dict in the case of 'single', 'frequency', 'source', 'both' is
        passed to :func:`emg3d.meshes.construct_mesh`; consult the
        corresponding documentation for more information. Parameters that are
        not provided are estimated from the model, grid, and survey using
        :func:`estimate_gridding_opts`, which documentation contains more
        information too.

        There are two notably differences to the parameters described in
        :func:`emg3d.meshes.construct_mesh`:

        - ``vector``: besides the normal possibility it can also be a string
          containing one or several of 'x', 'y', and 'z'. In these cases the
          corresponding dimension of the input mesh is provided as vector.
          See :func:`estimate_gridding_opts`.
        - ``expand``, in the format of ``[property_sea, property_air]``; if
          provided, the input model is expanded up to the seasurface with sea
          water, and an air layer is added. The actual height of the seasurface
          can be defined with the key ``seasurface``. See
          :func:`expand_grid_model`.

    solver_opts : dict, optional
        Passed through to :func:`emg3d.solver.solve`. The dict can contain any
        parameter that is accepted by the :func:`emg3d.solver.solve` except for
        `grid`, `model`, `sfield`, and `efield`.
        If not provided the following defaults are used:

        - `sslsolver=True`;
        - `semicoarsening=True`;
        - `linerelaxation=True`;
        - `verb=0` (yet warnings are capture and shown).

        Note that these defaults are different from the defaults in
        :func:`emg3d.solver.solve`. The defaults chosen here will be slower in
        many cases, but they are the most robust combination at which you can
        throw most things.

    max_workers : int
        The maximum number of processes that can be used to execute the
        given calls. Default is 4.

    verb : int; optional
        Level of verbosity. Default is 1.

        - -1: Error.
        - 0: Warning.
        - 1: Info.
        - 2: Debug.

    """

    # Gridding descriptions (for repr's).
    _gridding_descr = {
            'same': 'Same grid as for model',
            'single': 'A single grid for all sources and frequencies',
            'frequency': 'Frequency-dependent grids',
            'source': 'Source-dependent grids',
            'both': 'Frequency- and source-dependent grids',
            'input': 'A single, provided grid all sources/frequencies',
            'dict': 'Provided dict of frequency-/source-dependent grids',
            }

    def __init__(self, name, survey, grid, model, max_workers=4,
                 gridding='single', **kwargs):
        """Initiate a new Simulation instance."""

        # Store mandatory inputs (grid and model later).
        self.name = name
        self.survey = survey
        self.max_workers = max_workers
        self.gridding = gridding

        # Store optional inputs with defaults.
        self.verb = kwargs.pop('verb', 1)
        gridding_opts = kwargs.pop('gridding_opts', {}).copy()

        # Store solver options with defaults.
        # The slowest but most robust setting is used; also, verbosity is
        # switched off entirely, as warnings are captured differently.
        # Input overwrites all defaults if provided.
        self.solver_opts = {'sslsolver': True, 'semicoarsening': True,
                            'linerelaxation': True,  'verb': 0,
                            **kwargs.pop('solver_opts', {})}

        # Store original input nCz.
        self._input_nCz = kwargs.pop('_input_nCz', grid.nCz)

        # Ensure no kwargs left.
        if kwargs:
            raise TypeError(f"Unexpected **kwargs: {list(kwargs.keys())}")

        # Fixed surveys are not yet implemented.
        if self.survey.fixed:
            raise NotImplementedError(
                    "Simulation currently only implemented for "
                    "`survey.fixed=False`.")

        # Magnetic sources and receivers are not yet implemented.
        msrc = sum([not s.electric for s in survey.sources.values()]) > 0
        mrec = sum([not r.electric for r in survey.receivers.values()]) > 0
        if msrc or mrec:
            raise NotImplementedError("Simulation not yet implemented for "
                                      "magnetic sources and receivers.")

        # Initiate dictionaries and other values with None's.
        self._dict_grid = self._dict_initiate
        self._dict_model = self._dict_initiate
        self._dict_sfield = self._dict_initiate
        self._dict_efield = self._dict_initiate
        self._dict_hfield = self._dict_initiate
        self._dict_efield_info = self._dict_initiate
        self._gradient = None
        self._misfit = None

        # Check gridding_opts depending on gridding and act upon.
        if self.gridding == 'dict':
            self._dict_grid = gridding_opts
        elif self.gridding == 'input':
            self._grid_single = gridding_opts
        elif self.gridding == 'same':
            if gridding_opts:
                msg = "`gridding_opts` is not permitted if `gridding='same'`"
                raise TypeError(msg)
        else:

            # Expand model by water and air if required.
            expand = gridding_opts.pop('expand', None)
            if expand is not None:
                try:
                    interface = gridding_opts['seasurface']
                except KeyError as e:
                    msg = ("`gridding_opts['seasurface']` is required if "
                           "`gridding_opts['expand']` is provided.")
                    raise KeyError(msg) from e
                grid, model = expand_grid_model(grid, model, expand, interface)

            # Get automatic gridding input.
            # Estimate the parameters from survey and model if not provided.
            self.gridding_opts = estimate_gridding_opts(
                    gridding_opts, grid, model, survey, self._input_nCz)

        # Store grid and model.
        self.grid = grid
        self.model = model

        # Initiate synthetic data with NaN's.
        self.survey._data['synthetic'] = self.survey.data.observed*np.nan

        # `tqdm`-options; undocumented for the moment.
        # This is likely to change with regards to verbosity and logging.
        self._tqdm_opts = {
                'bar_format': '{desc}: {bar}{n_fmt}/{total_fmt}  [{elapsed}]',
                }

    def __repr__(self):
        return (f"*{self.__class__.__name__}* «{self.name}» "
                f"of {self.survey.__class__.__name__} «{self.survey.name}»\n\n"
                f"- {self.survey.__class__.__name__}: "
                f"{self.survey.shape[0]} sources; "
                f"{self.survey.shape[1]} receivers; "
                f"{self.survey.shape[2]} frequencies\n"
                f"- {self.model.__repr__()}\n"
                f"- Gridding: {self._gridding_descr[self.gridding]}; "
                f"{self._info_grids}")

    def _repr_html_(self):
        return (f"<h3>{self.__class__.__name__} «{self.name}»</h3>"
                f"of {self.survey.__class__.__name__} «{self.survey.name}»<ul>"
                f"<li>{self.survey.__class__.__name__}: "
                f"{self.survey.shape[0]} sources; "
                f"{self.survey.shape[1]} receivers; "
                f"{self.survey.shape[2]} frequencies</li>"
                f"<li>{self.model.__repr__()}</li>"
                f"<li>Gridding: {self._gridding_descr[self.gridding]}; "
                f"{self._info_grids}</li>"
                f"</ul>")

    def copy(self, what='computed'):
        """Return a copy of the Simulation.

        See `to_file` for more information regarding `what`.

        """
        return self.from_dict(self.to_dict(what, True))

    def to_dict(self, what='computed', copy=False):
        """Store the necessary information of the Simulation in a dict.

        See `to_file` for more information regarding `what`.

        """

        if what not in ['computed', 'results', 'all', 'plain']:
            raise TypeError(f"Unrecognized `what`: {what}")

        # If to_dict is called from to_file, it has a _what_to_file attribute.
        if hasattr(self, '_what_to_file'):
            what = self._what_to_file
            delattr(self, '_what_to_file')

        # Initiate dict.
        out = {'name': self.name, '__class__': self.__class__.__name__}

        # Add initiation parameters.
        out['survey'] = self.survey.to_dict()
        out['grid'] = self.grid.to_dict()
        out['model'] = self.model.to_dict()
        out['max_workers'] = self.max_workers
        out['gridding'] = self.gridding
        out['solver_opts'] = self.solver_opts

        # Put provided grids back on gridding.
        if self.gridding == 'input':
            out['gridding_opts'] = self._grid_single
        elif self.gridding == 'dict':
            out['gridding_opts'] = self._dict_grid
        elif self.gridding != 'same':
            out['gridding_opts'] = self.gridding_opts

        out['_input_nCz'] = self._input_nCz

        # Get required properties.
        store = []

        if what == 'all':
            store += ['_dict_grid', '_dict_model', '_dict_sfield',
                      '_dict_hfield']

        if what in ['computed', 'all']:
            store += ['_dict_efield', '_dict_efield_info']

        # store dicts.
        for name in store:
            out[name] = getattr(self, name)

        # store data.
        out['data'] = {}
        if what in ['computed', 'results', 'all']:
            for name in list(self.data.data_vars):
                # These two are stored in the Survey instance.
                if name not in ['observed', 'std']:
                    out['data'][name] = self.data.get(name)
            out['gradient'] = self._gradient
            out['misfit'] = self._misfit

        if copy:
            return deepcopy(out)
        else:
            return out

    @classmethod
    def from_dict(cls, inp):
        """Convert dictionary into :class:`Simulation` instance.


        Parameters
        ----------
        inp : dict
            Dictionary as obtained from :func:`Simulation.to_dict`.

        Returns
        -------
        obj : :class:`Simulation` instance

        """
        from emg3d import io

        try:
            # Initiate class.
            out = cls(
                    name=inp['name'],
                    survey=surveys.Survey.from_dict(inp['survey']),
                    grid=meshes.TensorMesh.from_dict(inp['grid']),
                    model=models.Model.from_dict(inp['model']),
                    max_workers=inp['max_workers'],
                    gridding=inp['gridding'],
                    solver_opts=inp['solver_opts'],
                    gridding_opts=inp.get('gridding_opts', {}),  # backw. comp.
                    _input_nCz=inp.get('_input_nCz', len(inp['grid']['hz']))
                    )

            # Add existing derived/computed properties.
            data = ['_dict_grid', '_dict_model', '_dict_sfield',
                    '_dict_hfield', '_dict_efield', '_dict_efield_info']
            for name in data:
                if name in inp.keys():
                    values = inp.get(name)

                    # Storing to_file makes strings out of the freq-keys.
                    # Undo this.
                    new_values = {}
                    for src, val in values.items():
                        new_values[src] = {}
                        for freq, v in val.items():
                            new_values[src][float(freq)] = val.get(freq)

                    # De-serialize Model, Field, and TensorMesh instances.
                    io._dict_deserialize(new_values)

                    setattr(out, name, new_values)

            data = ['gradient', 'misfit']
            for name in data:
                if name in inp.keys():
                    setattr(out, '_'+name, inp.get(name))

            # Add stored data (synthetic, residual, etc).
            for name in inp['data'].keys():
                out.data[name] = out.data.observed*inp['data'][name]

            return out

        except KeyError as e:
            raise KeyError(f"Variable {e} missing in `inp`.") from e

    def to_file(self, fname, what='computed', name='simulation', **kwargs):
        """Store Simulation to a file.

        Parameters
        ----------
        fname : str
            File name inclusive ending, which defines the used data format.
            Implemented are currently:

            - `.h5` (default): Uses `h5py` to store inputs to a hierarchical,
              compressed binary hdf5 file. Recommended file format, but
              requires the module `h5py`. Default format if ending is not
              provided or not recognized.
            - `.npz`: Uses `numpy` to store inputs to a flat, compressed binary
              file. Default format if `h5py` is not installed.
            - `.json`: Uses `json` to store inputs to a hierarchical, plain
              text file.

        what : str
            What to store. Currently implemented:

            - 'computed' (default):
              Stores all computed properties: electric fields and responses at
              receiver locations.
            - 'results':
              Stores only the response at receiver locations.
            - 'all':
              Stores everything.
            - 'plain':
              Only stores the plain Simulation (as initiated).

        name : str
            Name under which the survey is stored within the file.

        kwargs : Keyword arguments, optional
            Passed through to :func:`emg3d.io.save`.

        """
        from emg3d import io

        # Add what to self, will be removed in to_dict.
        self._what_to_file = what

        kwargs[name] = self                # Add simulation to dict.
        kwargs['collect_classes'] = False  # Ensure classes are not collected.
        # If verb is not defined, use verbosity of simulation.
        if 'verb' not in kwargs:
            kwargs['verb'] = self.verb

        io.save(fname, **kwargs)

    @classmethod
    def from_file(cls, fname, name='simulation', **kwargs):
        """Load Simulation from a file.

        Parameters
        ----------
        fname : str
            File name including extension. Used backend depends on the file
            extensions:

            - '.npz': numpy-binary
            - '.h5': h5py-binary (needs `h5py`)
            - '.json': json

        name : str
            Name under which the simulation is stored within the file.

        kwargs : Keyword arguments, optional
            Passed through to :func:`emg3d.io.load`.

        Returns
        -------
        simulation : :class:`Simulation`
            The simulation that was stored in the file.

        """
        from emg3d import io
        return io.load(fname, **kwargs)[name]

    # GET FUNCTIONS
    def get_grid(self, source, frequency):
        """Return computational grid of the given source and frequency."""
        freq = float(frequency)

        # Return grid if 'same' or if it exists already.
        if self._dict_grid[source][freq] is not None:
            return self._dict_grid[source][freq]

        # Act depending on gridding:
        if self.gridding == 'same':  # Same grid as for provided model.

            # Store link to grid.
            self._dict_grid[source][freq] = self.grid

        elif self.gridding == 'frequency':  # Frequency-dependent grids.

            # Initiate dict.
            if not hasattr(self, '_grid_frequency'):
                self._grid_frequency = {}

            # Get grid for this frequency if not yet computed.
            if freq not in self._grid_frequency.keys():

                # Get grid and store it.
                inp = {**self.gridding_opts, 'frequency': freq}
                self._grid_frequency[freq] = meshes.construct_mesh(**inp)

            # Store link to grid.
            self._dict_grid[source][freq] = self._grid_frequency[freq]

        elif self.gridding == 'source':  # Source-dependent grids.

            # Initiate dict.
            if not hasattr(self, '_grid_source'):
                self._grid_source = {}

            # Get grid for this source if not yet computed.
            if source not in self._grid_source.keys():

                # Get grid and store it.
                center = self.survey.sources[source].coordinates[:3]
                inp = {**self.gridding_opts, 'center': center}
                self._grid_source[source] = meshes.construct_mesh(**inp)

            # Store link to grid.
            self._dict_grid[source][freq] = self._grid_source[source]

        elif self.gridding == 'both':  # Src- & freq-dependent grids.

            # Get grid and store it.
            center = self.survey.sources[source].coordinates[:3]
            inp = {**self.gridding_opts, 'frequency': freq, 'center': center}
            self._dict_grid[source][freq] = meshes.construct_mesh(**inp)

        else:  # Use a single grid for all sources and receivers.
            # Default case; catches 'single' but also anything else.

            # Get grid if not yet computed.
            if not hasattr(self, '_grid_single'):

                # Get grid and store it.
                self._grid_single = meshes.construct_mesh(**self.gridding_opts)

            # Store link to grid.
            self._dict_grid[source][freq] = self._grid_single

        # Use recursion to return grid.
        return self.get_grid(source, frequency)

    def get_model(self, source, frequency):
        """Return model on the grid of the given source and frequency."""
        freq = float(frequency)

        # Get model if it is not stored yet.
        if self._dict_model[source][freq] is None:

            # Act depending on gridding:
            if self.gridding == 'same':  # Same grid as for provided model.

                # Store link to model.
                self._dict_model[source][freq] = self.model

            elif self.gridding == 'frequency':  # Frequency-dependent grids.

                # Initiate dict.
                if not hasattr(self, '_model_frequency'):
                    self._model_frequency = {}

                # Get model for this frequency if not yet computed.
                if freq not in self._model_frequency.keys():
                    self._model_frequency[freq] = self.model.interpolate2grid(
                            self.grid, self.get_grid(source, freq))

                # Store link to model.
                self._dict_model[source][freq] = self._model_frequency[freq]

            elif self.gridding == 'source':  # Source-dependent grids.

                # Initiate dict.
                if not hasattr(self, '_model_source'):
                    self._model_source = {}

                # Get model for this source if not yet computed.
                if source not in self._model_source.keys():
                    self._model_source[source] = self.model.interpolate2grid(
                            self.grid, self.get_grid(source, freq))

                # Store link to model.
                self._dict_model[source][freq] = self._model_source[source]

            elif self.gridding == 'both':  # Src- & freq-dependent grids.

                # Get model and store it.
                self._dict_model[source][freq] = self.model.interpolate2grid(
                            self.grid, self.get_grid(source, freq))

            else:  # Use a single grid for all sources and receivers.
                # Default case; catches 'single' but also anything else.

                # Get model if not yet computed.
                if not hasattr(self, '_model_single'):
                    self._model_single = self.model.interpolate2grid(
                            self.grid, self.get_grid(source, freq))

                # Store link to model.
                self._dict_model[source][freq] = self._model_single

        # Return model.
        return self._dict_model[source][freq]

    def get_sfield(self, source, frequency):
        """Return source field for given source and frequency."""
        freq = float(frequency)

        # Get source field if it is not stored yet.
        if self._dict_sfield[source][freq] is None:

            # Get source and source strength.
            src = self.survey.sources[source]
            if hasattr(src, 'strength'):
                strength = src.strength
            else:
                strength = 0

            sfield = fields.get_source_field(
                    grid=self.get_grid(source, frequency),
                    src=src.coordinates,
                    freq=frequency,
                    strength=strength)

            self._dict_sfield[source][freq] = sfield

        # Return source field.
        return self._dict_sfield[source][freq]

    def get_efield(self, source, frequency, **kwargs):
        """Return electric field for given source and frequency."""
        freq = float(frequency)

        # Get call_from_compute and ensure no kwargs are left.
        call_from_compute = kwargs.pop('call_from_compute', False)
        if kwargs:
            raise TypeError(f"Unexpected **kwargs: {list(kwargs.keys())}")

        # Compute electric field if it is not stored yet.
        if self._dict_efield[source][freq] is None:

            # Input parameters.
            solver_input = {
                **self.solver_opts,
                'grid': self.get_grid(source, freq),
                'model': self.get_model(source, freq),
                'sfield': self.get_sfield(source, freq),
                'return_info': True,
            }

            # Compute electric field.
            efield, info = solver.solve(**solver_input)

            # Store electric field and info.
            self._dict_efield[source][freq] = efield
            self._dict_efield_info[source][freq] = info

            # Clean corresponding hfield, so it will be recomputed.
            del self._dict_hfield[source][freq]
            self._dict_hfield[source][freq] = None

            # Get receiver coordinates.
            rec_coords = self.survey.rec_coords
            # For fixed surveys:
            # rec_coords = self.survey.rec_coords[source]

            # Extract data at receivers.
            resp = fields.get_receiver_response(
                    grid=self._dict_grid[source][freq],
                    field=self._dict_efield[source][freq],
                    rec=rec_coords
            )

            # Store the receiver response.
            self.data.synthetic.loc[source, :, freq] = resp

        # Return electric field.
        if call_from_compute:
            return (self._dict_efield[source][freq],
                    self._dict_efield_info[source][freq],
                    self.data.synthetic.loc[source, :, freq].data)
        else:
            return self._dict_efield[source][freq]

    def get_hfield(self, source, frequency, **kwargs):
        """Return magnetic field for given source and frequency."""
        freq = float(frequency)

        # If magnetic field not computed yet compute it.
        if self._dict_hfield[source][freq] is None:

            self._dict_hfield[source][freq] = fields.get_h_field(
                    self.get_grid(source, freq),
                    self.get_model(source, freq),
                    self.get_efield(source, freq, **kwargs))

        # Return magnetic field.
        return self._dict_hfield[source][freq]

    def get_efield_info(self, source, frequency):
        """Return the solver information of the corresponding computation."""
        return self._dict_efield_info[source][float(frequency)]

    # ASYNCHRONOUS COMPUTATION
    def _get_efield(self, inp):
        """Wrapper of `get_efield` for `concurrent.futures`."""
        return self.get_efield(*inp, call_from_compute=True)

    def compute(self, observed=False, **kwargs):
        """Compute efields asynchronously for all sources and frequencies.

        Parameters
        ----------
        observed : bool
            If True, it stores the current result also as observed model.
            This is usually done for pure forward modelling (not inversion).
            It will as such be stored within the survey. If the survey has
            either `relative_error` or `noise_floor`, random Gaussian noise
            with std will be added to the `data.observed` (not to
            data.synthetic). Also, data below the noise floor will be set to
            NaN.

        min_offset : float
            Default is 0.0. Data in `data.observed` where the offset <
            min_offset are set to NaN.

        """
        srcfreq = self._srcfreq.copy()

        # We remove the ones that were already computed.
        remove = []
        for src, freq in srcfreq:
            if self._dict_efield[src][freq] is not None:
                remove += [(src, freq)]
        for src, freq in remove:
            srcfreq.remove((src, freq))

        # Ensure grids, models, and source fields are computed.
        #
        # => This could be done within the field computation. But then it might
        #    have to be done multiple times even if 'single' or 'same' grid.
        #    Something to keep in mind.
        #    For `gridding='same'` it does not really matter.
        for src, freq in srcfreq:
            _ = self.get_grid(src, freq)
            _ = self.get_model(src, freq)
            _ = self.get_sfield(src, freq)

        # Initiate futures-dict to store output.
        out = process_map(
                self._get_efield,
                srcfreq,
                max_workers=self.max_workers,
                **{'desc': 'Compute efields', **self._tqdm_opts},
        )

        # Clean hfields, so they will be recomputed.
        del self._dict_hfield
        self._dict_hfield = self._dict_initiate

        # Loop over src-freq combinations to extract and store.
        warned = False  # Flag for warnings.
        for i, (src, freq) in enumerate(srcfreq):

            # Store efield.
            self._dict_efield[src][freq] = out[i][0]

            # Store solver info.
            info = out[i][1]
            self._dict_efield_info[src][freq] = info
            if info['exit'] != 0 and self.verb >= 0:
                if not warned:
                    print("Solver warnings:")
                    warned = True
                print(f"- Src {src}; {freq} Hz : {info['exit_message']}")

            # Store responses at receivers.
            self.data['synthetic'].loc[src, :, freq] = out[i][2]

        # If it shall be used as observed data save a copy.
        if observed:

            self.data['observed'] = self.data['synthetic'].copy()

            # Add noise noise_floor and/or relative_error given.
            if self.survey.standard_deviation is not None:

                # Create noise.
                std = self.survey.standard_deviation
                random = np.random.randn(self.survey.size*2)
                noise_re = std*random[::2].reshape(self.survey.shape)
                noise_im = std*random[1::2].reshape(self.survey.shape)

                # Add noise to observed data.
                self.data['observed'].data += noise_re + 1j*noise_im

            # Set data below the noise floor to NaN.
            if self.data.noise_floor is not None:
                min_amp = abs(self.data.synthetic.data) < self.data.noise_floor
                self.data['observed'].data[min_amp] = np.nan + 1j*np.nan

            # Set near-offsets to NaN.
            offsets = sl.norm(
                np.array(self.survey.rec_coords[:3])[:, None, :] -
                np.array(self.survey.src_coords[:3])[:, :, None],
                axis=0,
                check_finite=False,
            )
            min_off = offsets < kwargs.get('min_offset', 0.0)
            self.data['observed'].data[min_off] = np.nan + 1j*np.nan

    # DATA
    @property
    def data(self):
        """Shortcut to survey.data."""
        return self.survey.data

    # OPTIMIZATION
    @property
    def gradient(self):
        """Return the gradient of the misfit function.

        See :func:`emg3d.optimize.gradient`.

        """
        # Compute it if not stored already.
        if self._gradient is None:
            self._gradient = optimize.gradient(self)

        return self._gradient[:, :, :self._input_nCz]

    @property
    def misfit(self):
        """Return the misfit function.

        See :func:`emg3d.optimize.misfit`.

        """
        # Compute it if not stored already.
        if self._misfit is None:
            self._misfit = optimize.misfit(self)

        return self._misfit

    # UTILS
    def clean(self, what='computed'):
        """Clean part of the data base.

        Parameters
        ----------
        what : str
            What to clean. Currently implemented:

            - 'computed' (default):
              Removes all computed properties: electric and magnetic fields and
              responses at receiver locations.
            - 'keepresults':
              Removes everything  except for the responses at receiver
              locations.
            - 'all':
              Removes everything (leaves it plain as initiated).

        """

        if what not in ['computed', 'keepresults', 'all']:
            raise TypeError(f"Unrecognized `what`: {what}")

        clean = []

        if what in ['keepresults', 'all']:
            clean += ['_dict_grid', '_dict_model', '_dict_sfield']

        if what in ['computed', 'keepresults', 'all']:
            clean += ['_dict_efield', '_dict_efield_info', '_dict_hfield']

        # Clean dicts.
        for name in clean:
            delattr(self, name)
            setattr(self, name, self._dict_initiate)

        # Clean data.
        if what in ['computed', 'all']:
            for name in list(self.data.data_vars):
                if name not in ['observed', 'std']:
                    del self.data[name]
            self.data['synthetic'] = self.data.observed*np.nan
            for name in ['_gradient', '_misfit']:
                delattr(self, name)
                setattr(self, name, None)

    @property
    def _dict_initiate(self):
        """Returns a dict of the structure `dict[source][freq]=None`."""
        return {src: {freq: None for freq in self.survey.frequencies}
                for src in self.survey.sources.keys()}

    @property
    def _srcfreq(self):
        """Return list of all source-frequency pairs."""

        if getattr(self, '__srcfreq', None) is None:
            self.__srcfreq = list(
                    itertools.product(self.survey.sources.keys(),
                                      self.survey.frequencies))

        return self.__srcfreq

    @property
    def _info_grids(self):
        """Return a string with "min {- max}" grid size."""
        if self.gridding == 'same':
            min_nC = self.grid.nC
            min_vC = self.grid.vnC
            has_minmax = False
        elif self.gridding in ['single', 'input']:
            grid = self.get_grid(self._srcfreq[0][0], self._srcfreq[0][1])
            min_nC = grid.nC
            min_vC = grid.vnC
            has_minmax = False
        else:
            min_nC = 1e100
            max_nC = 0
            for src, freq in self._srcfreq:
                grid = self.get_grid(src, freq)
                if grid.nC > max_nC:
                    max_nC = grid.nC
                    max_vC = grid.vnC
                if grid.nC < min_nC:
                    min_nC = grid.nC
                    min_vC = grid.vnC
            has_minmax = min_nC != max_nC
        info = f"{min_vC[0]} x {min_vC[1]} x {min_vC[2]} ({min_nC:,})"
        if has_minmax:
            info += f" - {max_vC[0]} x {max_vC[1]} x {max_vC[2]} ({max_nC:,})"
        return info

    # BACKWARDS PROPAGATING FIELD
    def _get_bfields(self, inp):
        """Return back-propagated electric field for given inp (src, freq)."""

        # Input parameters.
        solver_input = {
            **self.solver_opts,
            'grid': self.get_grid(*inp),
            'model': self.get_model(*inp),
            'sfield': self._get_rfield(*inp),  # Residual field.
            'return_info': True,
        }

        # Compute and return back-propagated electric field.
        return solver.solve(**solver_input)

    def _bcompute(self):
        """Compute bfields asynchronously for all sources and frequencies."""

        # Initiate futures-dict to store output.
        out = process_map(
                self._get_bfields,
                self._srcfreq,
                max_workers=self.max_workers,
                **{'desc': 'Back-propagate ', **self._tqdm_opts},
        )

        # Store back-propagated electric field and info.
        if not hasattr(self, '_dict_bfield'):
            self._dict_bfield = self._dict_initiate
            self._dict_bfield_info = self._dict_initiate

        # Loop over src-freq combinations to extract and store.
        warned = False  # Flag for warnings.
        for i, (src, freq) in enumerate(self._srcfreq):

            # Store bfield.
            self._dict_bfield[src][freq] = out[i][0]

            # Store solver info.
            info = out[i][1]
            self._dict_bfield_info[src][freq] = info
            if info['exit'] != 0 and self.verb >= 0:
                if not warned:
                    print("Solver warnings:")
                    warned = True
                print(f"- Src {src}; {freq} Hz : {info['exit_message']}")

    def _get_rfield(self, source, frequency):
        """Return residual source field for given source and frequency."""

        freq = float(frequency)
        grid = self.get_grid(source, frequency)

        # Initiate empty field
        ResidualField = fields.SourceField(grid, freq=frequency)

        # Loop over receivers, input as source.
        for name, rec in self.survey.receivers.items():

            # Strength: in get_source_field the strength is multiplied with
            # iwmu; so we undo this here.
            residual = self.data.residual.loc[source, name, freq].data
            if np.isnan(residual):
                continue
            strength = residual.conj()
            strength *= self.data.weights.loc[source, name, freq].data.conj()
            strength /= ResidualField.smu0

            # If strength is zero (very unlikely), get_source_field would
            # return a normalized field for a unit source. However, in this
            # case we do not want that.
            if strength != 0:
                ResidualField += fields.get_source_field(
                    grid=grid,
                    src=rec.coordinates,
                    freq=frequency,
                    strength=strength,
                )

        return ResidualField


# HELPER FUNCTIONS
def expand_grid_model(grid, model, expand, interface):
    """Expand model and grid according to provided parameters.

    Expand the grid and corresponding model in positive z-direction from the
    end of the grid to the interface with ``expand[0]``, and a 100 m thick
    layer above the interface with ``expand[1]``.

    The provided properties are taken as isotropic; ``mu_r`` and ``epsilon_r``
    are expanded with ones, if necessary.

    Usually the ``interface`` is the sea-surface, and ``expand`` is therefore
    ``[property_sea, property_air]``.

    Parameters
    ----------
    grid : :class:`emg3d.meshes.TensorMesh`
        The grid.

    model : :class:`emg3d.models.Model`
        The model.

    expand : list
        The two properties below and above the interface:
        ``[below_interface, above_interface]``.

    interface : float
        Interface between the two properties in ``expand``.


    Returns
    -------
    grid : :class:`emg3d.meshes.TensorMesh`
        Expanded grid.

    model : :class:`emg3d.models.Model`
        Expanded model.

    """

    def extend_property(prop, add_values, nadd):
        """Expand property `model.prop`, IF it is not None."""

        if getattr(model, '_'+prop) is None:
            prop_ext = None

        else:
            prop_ext = np.zeros((grid.nCx, grid.nCy, grid.nCz+nadd))
            prop_ext[:, :, :-nadd] = getattr(model, prop)
            if nadd == 2:
                prop_ext[:, :, -2] = add_values[0]
            prop_ext[:, :, -1] = add_values[1]

        return prop_ext

    # Initiate.
    nzadd = 0
    hz_ext = grid.hz

    # Fill-up property_below.
    if grid.vectorNz[-1] < interface-0.05:  # At least 5 cm.
        hz_ext = np.r_[hz_ext, interface-grid.vectorNz[-1]]
        nzadd += 1

    # Add 100 m of property_above.
    if grid.vectorNz[-1] <= interface+0.001:  # +1mm
        hz_ext = np.r_[hz_ext, 100]
        nzadd += 1

    if nzadd > 0:
        # Extend properties.
        property_x = extend_property('property_x', expand, nzadd)
        property_y = extend_property('property_y', expand, nzadd)
        property_z = extend_property('property_z', expand, nzadd)
        mu_r = extend_property('mu_r', [1, 1], nzadd)
        epsilon_r = extend_property('epsilon_r', [1, 1], nzadd)

        # Create extended grid and model.
        grid = meshes.TensorMesh(
                [grid.hx, grid.hy, hz_ext], x0=grid.x0)
        model = models.Model(
                grid, property_x, property_y, property_z, mu_r,
                epsilon_r, mapping=model.map.name)

    return grid, model


def estimate_gridding_opts(gridding_opts, grid, model, survey, input_nCz=None):
    """Estimate parameters for automatic gridding.

    Automatically determines the required gridding options from the provided
    grid, model, and survey, if they are not provided in ``gridding_opts``.

    The dict ``gridding_opts`` can contain any input parameter taken by
    :func:`emg3d.meshes.construct_mesh`, see the corresponding documentation
    for more details with regards to the possibilities.

    Different keys of ``gridding_opts`` are treated differently:

    - The following parameters are estimated from the ``model`` if not
      provided:

      - ``mapping``: taken from model
      - ``properties``: volume averages (of x, y, and z-directed properties) on
        log10 scale of the outermost layer in a given direction.

    - The following parameters are estimated from the ``survey`` if not
      provided:

      - ``frequency``: average (on log10-scale) of all frequencies.
      - ``center``: center of all sources.
      - ``domain`` from ``vector`` if provided, or in x/y-directions: extent of
        sources and receivers, ensuring ratio of 3.
      - ``domain`` from ``vector`` if provided, or in z-direction: extent of
        sources and receivers, ensuring ratio of 2 to horizontal dimension;
        1/10 tenth up, 9/10 down.

      The ratio means that it is enforced that the survey dimension in x or
      y-direction is not smaller than a third of the survey dimension in the
      other direction. If not, the smaller dimension is expanded symmetrically.
      Similarly in the vertical direction, which must be at least half the
      dimension of the maximum horizontal dimension or 5 km, whatever is
      smaller. Otherwise it is expanded in a ration of 9 parts downwards, one
      part upwards.

    - The following parameter is taken from the ``grid`` if provided as a
      string:

      - ``vector``: This is the only real "difference" to the default
        ``gridding_opts``-dict. The normal input is accepted, but it can also
        be a string contain any combination of 'x', 'y', and 'z'. All
        directions contained in this string are then taken from the provided
        grid. E.g., if ``gridding_opts['vector']='xz'`` it will take the x- and
        z-directed vectors from the grid.

    - The following parameters are simply passed along if they are provided,
      nothing is done otherwise:

      - ``vector``
      - ``stretching``
      - ``seasurface``
      - ``cell_numbers``
      - ``lambda_factor``
      - ``max_buffer``
      - ``min_width_limits``
      - ``min_width_pps``
      - ``verb``


    Parameters
    ----------
    gridding_opts : dict
        Containing input parameters to provide to
        :func:`emg3d.meshes.contsruct_mesh`. See the corresponding
        documentation and the explanations above.

    grid : :class:`emg3d.meshes.TensorMesh`
        The grid.

    model : :class:`emg3d.models.Model`
        The model.

    survey : :class:`emg3d.surveys.Survey`
        The survey.

    input_nCz : int, optional
        If :func:`expand_grid_model` was used, `input_nCz` corresponds to the
        original ``grid.nCz``.


    Returns
    -------
    gridding_opts : dict
        Dict to provide to :func:`emg3d.meshes.contsruct_mesh`.

    """
    # Initiate new gridding_opts.
    gopts = {}

    # Optional values that we only include if provided.
    for name in ['stretching', 'seasurface', 'cell_numbers', 'lambda_factor',
                 'max_buffer', 'min_width_limits', 'min_width_pps', 'verb']:
        if name in gridding_opts.keys():
            gopts[name] = gridding_opts.pop(name)

    # Mapping defaults to model map.
    gopts['mapping'] = gridding_opts.pop('mapping', model.map)

    # Frequency defaults to average frequency (log10).
    freq = 10**np.mean(np.log10(survey.frequencies))
    gopts['frequency'] = gridding_opts.pop('frequency', freq)

    # Center defaults to center of all sources.
    center = tuple([np.mean(survey.src_coords[i]) for i in range(3)])
    gopts['center'] = gridding_opts.pop('center', center)

    # Vector.
    vector = gridding_opts.pop('vector', None)
    if isinstance(vector, str):
        # If vector is a string we take the corresponding vectors from grid.
        vector = (
                grid.vectorNx if 'x' in vector.lower() else None,
                grid.vectorNy if 'y' in vector.lower() else None,
                grid.vectorNz[:input_nCz] if 'z' in vector.lower() else None,
        )
        gopts['vector'] = vector

    elif vector is not None:
        # In this case vector was provided, and we include it like this.
        gopts['vector'] = vector

    # Properties defaults to model averages (AFTER model expansion).
    properties = gridding_opts.pop('properties', None)
    if properties is None:

        # Get map (in principle the map in gridding_opts could be different
        # from the map in the model).
        m = gopts['mapping']
        if isinstance(m, str):
            m = getattr(maps, 'Map'+m)()

        # Mean of all values (x, y, z).
        def get_mean(ix, iy, iz):
            """Get mean; currently based on the averaged property."""

            # Get volume factor.
            vfact = grid.vol.reshape(grid.vnC, order='F')
            vfact = vfact[ix, iy, iz].ravel()/vfact[ix, iy, iz].sum()

            # Collect all x (y, z) values.
            data = np.array([])
            for p in ['x', 'y', 'z']:
                prop = getattr(model, 'property_'+p)
                if getattr(model, '_property_'+p) is None:
                    continue
                elif prop.ndim == 1:
                    data = np.r_[data, model.map.backward(prop)]
                else:
                    prop = model.map.backward(prop[ix, iy, iz].ravel())
                    data = np.r_[data, np.sum(vfact*prop)]

            # Return mean, on a log10-scale.
            return m.forward(10**np.mean(np.log10(data)))

        # Buffer properties.
        xneg = get_mean(0, slice(None), slice(None))
        xpos = get_mean(-1, slice(None), slice(None))
        yneg = get_mean(slice(None), 0, slice(None))
        ypos = get_mean(slice(None), -1, slice(None))
        zneg = get_mean(slice(None), slice(None), 0)
        zpos = get_mean(slice(None), slice(None), -1)

        # Source property.
        ix = np.argmin(abs(grid.vectorNx - gopts['center'][0]))
        iy = np.argmin(abs(grid.vectorNy - gopts['center'][1]))
        iz = np.argmin(abs(grid.vectorNz - gopts['center'][2]))
        source = get_mean(ix, iy, iz)

        properties = [source, xneg, xpos, yneg, ypos, zneg, zpos]

    gopts['properties'] = properties

    # Domain; default taken from survey.
    domain = gridding_opts.pop('domain', None)
    if domain is None:

        def add_ten(i):
            """Return ([min, max], dim) of inp.

            Take it from vector if provided, else from survey, adding 5% on
            each side).
            """
            if vector is not None and vector[i] is not None:
                dim = [np.min(vector[i]), np.max(vector[i])]
                diff = np.diff(dim)[0]
                get_it = False
            else:
                inp = np.r_[survey.src_coords[i], survey.rec_coords[i]]
                dim = [min(inp), max(inp)]
                diff = np.diff(dim)[0]
                dim = [min(inp)-diff/20, max(inp)+diff/20]
                diff = np.diff(dim)[0]
                get_it = True
            return dim, diff, get_it

        xdim, xdiff, get_x = add_ten(0)
        ydim, ydiff, get_y = add_ten(1)
        zdim, zdiff, get_z = add_ten(2)

        # Ensure the ratio xdim:ydim is at most 3.
        if get_y and xdiff/ydiff > 3:
            diff = round((xdiff/3.0 - ydiff)/2.0)
            ydim = [ydim[0]-diff, ydim[1]+diff]
        elif get_x and ydiff/xdiff > 3:
            diff = round((ydiff/3.0 - xdiff)/2.0)
            xdim = [xdim[0]-diff, xdim[1]+diff]

        # Ensure the ratio zdim:horizontal is at most 2.
        hdist = min(10000, max(xdiff, ydiff))
        if get_z and hdist/zdiff > 2:
            diff = round((hdist/2.0 - zdiff)/10.0)
            zdim = [zdim[0]-9*diff, zdim[1]+diff]

        # Collect
        domain = (xdim, ydim, zdim)

    gopts['domain'] = domain

    # Ensure no gridding_opts left.
    if gridding_opts:
        raise TypeError(
                f"Unexpected gridding_opts: {list(gridding_opts.keys())}")

    # Return gridding_opts.
    return gopts
