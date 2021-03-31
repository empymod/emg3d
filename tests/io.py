import pytest
import numpy as np
from numpy.testing import assert_allclose

import emg3d
from emg3d import io

from . import helpers

# Soft dependencies
try:
    import h5py
except ImportError:
    h5py = False
try:
    import xarray
except ImportError:
    xarray = None


def test_save_and_load(tmpdir, capsys):

    # Create some dummy data
    grid = emg3d.meshes.TensorMesh(
            [np.array([2, 2]), np.array([3, 4]), np.array([0.5, 2])],
            np.zeros(3))

    # TODO Add conditional discretize test, if installed
    # grid2 = discretize.TensorMesh()

    # Some field.
    field = emg3d.Field(grid)
    ne = grid.n_edges_x + grid.n_edges_y + grid.n_edges_z
    field.field = np.arange(ne)+1j*np.ones(ne)

    # Some model.
    property_x = helpers.dummy_field(*grid.shape_cells, False)
    property_y = property_x/2.0
    property_z = property_x*1.4
    mu_r = property_x*1.11
    model = emg3d.Model(grid, property_x, property_y, property_z, mu_r=mu_r)

    # Save it.
    io.save(tmpdir+'/test.npz', emg3d=grid, model=model,  # discretize=grid2,
            a=None, b=True, field=field, what={'f': field.fx, 12: 12})
    outstr, _ = capsys.readouterr()
    assert 'Data saved to «' in outstr
    assert emg3d.__version__ in outstr

    # Save it with other verbosity.
    _, _ = capsys.readouterr()
    io.save(tmpdir+'/testverb.npz', a=None, b=True, verb=0)
    outstr, _ = capsys.readouterr()
    assert outstr == ""
    out = io.save(tmpdir+'/testverb.npz', a=None, b=True, verb=-1)
    assert 'Data saved to «' in out

    # Load it.
    out_npz = io.load(str(tmpdir+'/test.npz'), allow_pickle=True)
    outstr, _ = capsys.readouterr()
    assert 'Data loaded from «' in outstr
    assert 'test.npz' in outstr
    assert emg3d.__version__ in outstr

    assert out_npz['model'] == model
    assert_allclose(field.fx, out_npz['field'].fx)
    assert_allclose(grid.cell_volumes, out_npz['emg3d'].cell_volumes)
    # assert_allclose(grid.cell_volumes, out_npz['discretize'].cell_volumes)
    assert_allclose(out_npz['what']['f'], field.fx)
    assert out_npz['b'] is True

    # Load it with other verbosity.
    _, _ = capsys.readouterr()
    out = io.load(tmpdir+'/testverb.npz', verb=0)
    outstr, _ = capsys.readouterr()
    assert outstr == ""
    out, out_str = io.load(tmpdir+'/testverb.npz', verb=-1)
    assert 'Data loaded from «' in out_str

    # Check message from loading another file
    data = io._dict_serialize({'meshes': grid})
    fdata = io._dict_flatten(data)
    del fdata['meshes>hx']

    np.savez_compressed(tmpdir+'/test2.npz', **fdata)
    with pytest.warns(UserWarning, match="Could not de-serialize"):
        _ = io.load(str(tmpdir+'/test2.npz'), allow_pickle=True)
        outstr, _ = capsys.readouterr()
        assert "[version/format/date unknown; not created by emg3d]." in outstr

    # Unknown keyword.
    with pytest.raises(TypeError, match="Unexpected "):
        io.load('ttt.npz', stupidkeyword='a')

    # Unknown extension.
    with pytest.raises(ValueError, match="Unknown extension '.abc'"):
        io.save(tmpdir+'/testwrongextension.abc', something=1)
    with pytest.raises(ValueError, match="Unknown extension '.abc'"):
        io.load(tmpdir+'/testwrongextension.abc')

    # Test h5py.
    if h5py:
        io.save(tmpdir+'/test.h5', emg3d=grid,  # discretize=grid2,
                a=1.0, b=1+1j, c=True,
                d=['1', '2', '3'],
                model=model, field=field, what={'f': field.fx})
        out_h5 = io.load(str(tmpdir+'/test.h5'))
        assert out_h5['model'] == model
        assert out_h5['a'] == 1.0
        assert out_h5['b'] == 1+1j
        assert out_h5['c'] is True
        assert out_h5['d'] == ['1', '2', '3']
        assert_allclose(field.fx, out_h5['field'].fx)
        assert_allclose(grid.cell_volumes, out_h5['emg3d'].cell_volumes)
        # assert_allclose(grid.cell_volumes, out_h5['discretize'].cell_volumes)
        assert_allclose(out_h5['what']['f'], field.fx)

        # Currently npz/h5/json DO NOT work the same (tuples, lists,...) TODO
        # assert helpers.compare_dicts(out_h5, out_npz) is True
    else:
        with pytest.raises(ImportError):
            io.save(tmpdir+'/test.h5', grid=grid)
        with pytest.raises(ImportError):
            io.load(str(tmpdir+'/test-h5.h5'))

    # Test json.
    io.save(tmpdir+'/test.json', emg3d=grid,  # discretize=grid2,
            a=1.0, b=1+1j, model=model, field=field, what={'f': field.fx})
    out_json = io.load(str(tmpdir+'/test.json'))
    assert out_json['model'] == model
    assert out_json['a'] == 1.0
    assert out_json['b'] == 1+1j
    assert_allclose(field.fx, out_json['field'].fx)
    assert_allclose(grid.cell_volumes, out_json['emg3d'].cell_volumes)
    # assert_allclose(grid.cell_volumes, out_json['discretize'].cell_volumes)
    assert_allclose(out_json['what']['f'], field.fx)

    # Currently npz/h5/json DO NOT work the same (tuples, lists,...) TODO
    # assert helpers.compare_dicts(out_json, out_npz) is True


def test_known_classes(tmpdir):

    frequency = 1.0
    grid = emg3d.TensorMesh([[2, 2], [3, 4], [0.5, 2]], (0, 0, 0))
    field = emg3d.Field(grid)
    model = emg3d.Model(grid, 1)
    pointdip = emg3d.TxElectricDipole((0, 1000, -950, 0, 0))

    out = {
        'TensorMesh': grid,
        'Model': model,
        'Field': field,
        'Dipole': pointdip,
    }

    if xarray:
        survey = emg3d.Survey((-0.5, 0.5, 1000, 1000, -950, -950),
                              (0, 1000, -950, 0, 0), frequency)
        simulation = emg3d.Simulation(
                survey, grid, model, gridding='same')
        out['Survey'] = survey
        out['Simulation'] = simulation

    # Simple primitive test to see if it can (de)serialize all known classes.
    def test_it(ext):
        io.save(tmpdir+'/test.'+ext, **out)
        inp = io.load(tmpdir+'/test.'+ext)
        del inp['_date']
        del inp['_version']
        del inp['_format']
        assert out.keys() == inp.keys()

    # Run through all format.
    test_it('npz')
    test_it('json')
    if h5py:
        test_it('h5')