import pytest
import shelve
import numpy as np
from scipy import constants
from os.path import join, dirname
from numpy.testing import assert_allclose, assert_array_equal

# Import soft dependencies.
try:
    import discretize
    # Backwards compatibility; remove latest for version 1.0.0.
    dv = discretize.__version__.split('.')
    if int(dv[0]) == 0 and int(dv[1]) < 6:
        discretize = None
except ImportError:
    discretize = None

from emg3d import io, meshes, models, fields, solver

# Data generated with tests/create_data/regression.py
REGRES = io.load(join(dirname(__file__), 'data/regression.npz'))


def get_h(ncore, npad, width, factor):
    """Get cell widths for TensorMesh."""
    pad = ((np.ones(npad)*np.abs(factor))**(np.arange(npad)+1))*width
    return np.r_[pad[::-1], np.ones(ncore)*width, pad]


def create_dummy(nx, ny, nz, imag=True):
    """Return complex dummy arrays of shape nx*ny*nz.

    Numbers are from 1..nx*ny*nz for the real part, and 1/100 of it for the
    imaginary part.

    """
    if imag:
        out = np.arange(1., nx*ny*nz+1) + 1j*np.arange(1., nx*ny*nz+1)/100.
    else:
        out = np.arange(1., nx*ny*nz+1)
    return out.reshape(nx, ny, nz)


class TestField:
    grid = meshes.TensorMesh([[.5, 8], [1, 4], [2, 8]], (0, 0, 0))

    ex = create_dummy(*grid.shape_edges_x, imag=True)
    ey = create_dummy(*grid.shape_edges_y, imag=True)
    ez = create_dummy(*grid.shape_edges_z, imag=True)

    # Test the views
    field = np.r_[ex.ravel('F'), ey.ravel('F'), ez.ravel('F')]

    def test_basic(self):
        ee = fields.Field(self.grid, self.field)
        assert_allclose(ee.field, self.field)
        assert_allclose(ee.fx, self.ex)
        assert_allclose(ee.fy, self.ey)
        assert_allclose(ee.fz, self.ez)
        assert ee.smu0 is None
        assert ee.sval is None
        assert ee.frequency is None
        assert ee.field.dtype == self.field.dtype

        # Test amplitude and phase.

        assert_allclose(ee.fx.amp(), np.abs(ee.fx))
        assert_allclose(ee.fy.pha(unwrap=False), np.angle(ee.fy))

        # Test the other possibilities to initiate a Field-instance.
        frequency = 1.0
        ee3 = fields.Field(self.grid, frequency=frequency,
                           dtype=self.field.dtype)
        assert ee.field.size == ee3.field.size
        assert ee.field.dtype == np.complex128
        assert ee3.frequency == frequency

        # Try setting values
        ee3.field = ee.field
        assert ee3.smu0/ee3.sval == constants.mu_0
        assert ee != ee3  # First has no frequency
        ee3.fx = ee.fx
        ee3.fy = ee.fy
        ee3.fz = ee.fz

        # Negative
        ee4 = fields.Field(self.grid, frequency=-frequency)
        assert ee.field.size == ee4.field.size
        assert ee4.field.dtype == np.float64
        assert ee4.frequency == frequency
        assert ee4._frequency == -frequency
        assert ee4.smu0/ee4.sval == constants.mu_0

    def test_dtype(self):
        with pytest.raises(ValueError, match="must be f>0"):
            _ = fields.Field(self.grid, frequency=0.0)

        with pytest.warns(np.ComplexWarning, match="Casting complex values"):
            lp = fields.Field(self.grid, self.field, frequency=-1)
        assert lp.field.dtype == np.float64

        ignore = fields.Field(self.grid, frequency=-1, dtype=np.int64)
        assert ignore.field.dtype == np.float64

        ignore = fields.Field(self.grid, self.field, dtype=np.int64)
        assert ignore.field.dtype == np.complex128

        respected = fields.Field(self.grid, dtype=np.int64)
        assert respected.field.dtype == np.int64

        default = fields.Field(self.grid)
        assert default.field.dtype == np.complex128

    def test_copy_dict(self, tmpdir):
        ee = fields.Field(self.grid, self.field)
        # Test copy
        e2 = ee.copy()
        assert ee == e2
        assert_allclose(ee.fx, e2.fx)
        assert_allclose(ee.fy, e2.fy)
        assert_allclose(ee.fz, e2.fz)
        assert not np.may_share_memory(ee.field, e2.field)

        edict = ee.to_dict()
        del edict['grid']
        with pytest.raises(KeyError, match="'grid'"):
            fields.Field.from_dict(edict)

        # Ensure it can be pickled.
        with shelve.open(tmpdir+'/test') as db:
            db['field'] = ee
        with shelve.open(tmpdir+'/test') as db:
            test = db['field']
        assert test == ee

    def test_interpolate_to_grid(self):
        # We only check here that it gives the same as calling the function
        # itself; the rest should be tested in interpolate().
        grid1 = meshes.TensorMesh(
                [np.ones(8), np.ones(8), np.ones(8)], (0, 0, 0))
        grid2 = meshes.TensorMesh([[2, 2, 2, 2], [3, 3], [4, 4]], (0, 0, 0))
        ee = fields.Field(grid1)
        ee.field = np.ones(ee.field.size) + 2j*np.ones(ee.field.size)
        e2 = ee.interpolate_to_grid(grid2)
        assert_allclose(e2.field, 1+2j)

    def test_get_receiver(self):
        # We only check here that it gives the same as calling the function
        # itself; the rest should be tested in get_receiver().
        grid1 = meshes.TensorMesh(
                [np.ones(8), np.ones(8), np.ones(8)], (0, 0, 0))
        ee = fields.Field(grid1)
        ee.field = np.arange(ee.field.size) + 2j*np.arange(ee.field.size)
        resp = ee.get_receiver((4, 4, 4, 0, 0))
        print(80*'=')
        print(resp)
        print(80*'=')
        assert_allclose(resp, 323.5 + 647.0j)


class TestGetSourceField:
    def test_get_source_field(self, capsys):
        src = [100, 200, 300, 27, 31]
        h = np.ones(4)
        grid = meshes.TensorMesh([h*200, h*400, h*800], (-450, -850, -1650))
        freq = 1.2458

        sfield = fields.get_source_field(grid, src, freq, strength=1+1j)
        assert_array_equal(sfield.strength, complex(1+1j))

        sfield = fields.get_source_field(grid, src, freq, strength=0)
        assert_array_equal(sfield.strength, float(0))
        iomegamu = 2j*np.pi*freq*constants.mu_0

        # Check number of edges
        assert 4 == sfield.fx[sfield.fx != 0].size
        assert 4 == sfield.fy[sfield.fy != 0].size
        assert 4 == sfield.fz[sfield.fz != 0].size

        # Check source cells
        h = np.cos(np.deg2rad(src[4]))
        y = np.sin(np.deg2rad(src[3]))*h
        x = np.cos(np.deg2rad(src[3]))*h
        z = np.sin(np.deg2rad(src[4]))
        assert_allclose(np.sum(sfield.fx/x/iomegamu).real, -1)
        assert_allclose(np.sum(sfield.fy/y/iomegamu).real, -1)
        assert_allclose(np.sum(sfield.fz/z/iomegamu).real, -1)
        assert sfield._frequency == freq
        assert sfield.frequency == freq
        assert_allclose(sfield.smu0, -iomegamu)

        # Put source on final node, should still work.
        src = [grid.nodes_x[0], grid.nodes_x[0]+1,
               grid.nodes_y[-1]-1, grid.nodes_y[-1],
               grid.nodes_z[0], grid.nodes_z[0]+1]
        sfield = fields.get_source_field(grid, src, freq)
        tot_field = np.linalg.norm(
                [np.sum(sfield.fx), np.sum(sfield.fy), np.sum(sfield.fz)])
        assert_allclose(tot_field/np.abs(np.sum(iomegamu)), 1.0)

        out, _ = capsys.readouterr()  # Empty capsys

        # Provide wrong source definition. Ensure it fails.
        with pytest.raises(ValueError, match='Source is wrong defined'):
            sfield = fields.get_source_field(grid, [0, 0, 0, 0], 1)

        # Put finite dipole of zero length. Ensure it fails.
        with pytest.raises(ValueError, match='Provided finite dipole has no '):
            src = [0, 0, 100, 100, -200, -200]
            sfield = fields.get_source_field(grid, src, 1)

        # Same for Laplace domain
        src = [100, 200, 300, 27, 31]
        h = np.ones(4)
        grid = meshes.TensorMesh([h*200, h*400, h*800], (-450, -850, -1650))
        freq = 1.2458
        sfield = fields.get_source_field(grid, src, -freq)
        smu = freq*constants.mu_0

        # Check number of edges
        assert 4 == sfield.fx[sfield.fx != 0].size
        assert 4 == sfield.fy[sfield.fy != 0].size
        assert 4 == sfield.fz[sfield.fz != 0].size

        # Check source cells
        h = np.cos(np.deg2rad(src[4]))
        y = np.sin(np.deg2rad(src[3]))*h
        x = np.cos(np.deg2rad(src[3]))*h
        z = np.sin(np.deg2rad(src[4]))
        assert_allclose(np.sum(sfield.fx/x/smu), -1)
        assert_allclose(np.sum(sfield.fy/y/smu), -1)
        assert_allclose(np.sum(sfield.fz/z/smu), -1)
        assert sfield._frequency == -freq
        assert sfield.frequency == freq
        assert_allclose(sfield.smu0, -freq*constants.mu_0)

    def test_arbitrarily_shaped_source(self):
        h = np.ones(4)
        grid = meshes.TensorMesh([h*200, h*400, h*800], [-400, -800, -1600])
        freq = 1.11
        strength = np.pi
        src = (0, 0, 0, 0, 90)

        with pytest.raises(ValueError, match='All source coordinates must ha'):
            fields.get_source_field(grid, ([1, 2], 1, 1), freq, strength)

        # Manually
        sman = fields.Field(grid, frequency=freq)
        src4xxyyzz = [
            np.r_[src[0]-0.5, src[0]+0.5, src[1]-0.5,
                  src[1]-0.5, src[2], src[2]],
            np.r_[src[0]+0.5, src[0]+0.5, src[1]-0.5,
                  src[1]+0.5, src[2], src[2]],
            np.r_[src[0]+0.5, src[0]-0.5, src[1]+0.5,
                  src[1]+0.5, src[2], src[2]],
            np.r_[src[0]-0.5, src[0]-0.5, src[1]+0.5,
                  src[1]-0.5, src[2], src[2]],
        ]
        for srcl in src4xxyyzz:
            sman.field += fields.get_source_field(
                    grid, srcl, freq, strength).field

        # Computed
        src5xyz = (
            [src[0]-0.5, src[0]+0.5, src[0]+0.5, src[0]-0.5, src[0]-0.5],
            [src[1]-0.5, src[1]-0.5, src[1]+0.5, src[1]+0.5, src[1]-0.5],
            [src[2], src[2], src[2], src[2], src[2]]
        )
        scomp = fields.get_source_field(grid, src5xyz, freq, strength)

        # Computed 2
        with pytest.raises(TypeError, match='Unexpected'):
            fields.get_source_field(grid, src, freq, strength, whatever=True)

        assert sman == scomp

        # Normalized
        sman = fields.Field(grid, frequency=freq)
        for srcl in src4xxyyzz:
            sman.field += fields.get_source_field(grid, srcl, freq, 0.25).field
        scomp = fields.get_source_field(grid, src5xyz, freq)
        assert sman == scomp

    def test_source_field(self):
        # Create some dummy data
        grid = meshes.TensorMesh(
                [np.array([.5, 8]), np.array([1, 4]), np.array([2, 8])],
                np.zeros(3))

        freq = np.pi
        ss = fields.Field(grid, frequency=freq)
        assert_allclose(ss.smu0, -2j*np.pi*freq*constants.mu_0)

        # Check 0 Hz frequency.
        with pytest.raises(ValueError, match='`frequency` must be f>0'):
            ss = fields.Field(grid, frequency=0)

        sdict = ss.to_dict()
        del sdict['grid']
        with pytest.raises(KeyError, match="'grid'"):
            fields.Field.from_dict(sdict)


def test_get_receiver():
    # Check cubic spline runs fine (NOT CHECKING ACTUAL VALUES!.
    grid = meshes.TensorMesh(
            [np.ones(4), np.array([1, 2, 3, 1]), np.array([2, 1, 1, 1])],
            [0, 0, 0])
    field = fields.Field(grid)
    field.field = np.ones(field.field.size) + 1j*np.ones(field.field.size)

    grid = meshes.TensorMesh(
            [np.ones(6), np.array([1, 1, 2, 3, 1]), np.array([1, 2, 1, 1, 1])],
            [-1, -1, -1])
    efield = fields.Field(grid, frequency=1)
    efield.field = np.ones(efield.field.size) + 1j*np.ones(efield.field.size)

    # Provide wrong rec_loc input:
    with pytest.raises(ValueError, match='`receiver` needs to be in the form'):
        fields.get_receiver(efield, (1, 1, 1))

    # Provide particular field instead of field instance:
    with pytest.raises(ValueError, match='`field` must be a `Field`-inst'):
        fields.get_receiver(efield.fx, (1, 1, 1, 0, 0))

    # Coarse check with emg3d.solve and empymod.
    x = np.array([400, 450, 500, 550])
    rec = (x, x*0, 0, 20, 70)
    res = 0.3
    src = (0, 0, 0, 0, 0)
    freq = 10

    grid = meshes.construct_mesh(
            frequency=freq,
            center=(0, 0, 0),
            properties=res,
            domain=[[0, 1000], [-25, 25], [-25, 25]],
            min_width_limits=20,
    )

    model = models.Model(grid, res)
    sfield = fields.get_source_field(grid, src, freq)
    efield = solver.solve(model, sfield, semicoarsening=True,
                          sslsolver=True, linerelaxation=True, verb=1)

    # epm = empymod.bipole(src, rec, [], res, freq, verb=1)
    epm = np.array([-1.27832028e-11+1.21383502e-11j,
                    -1.90064149e-12+7.51937145e-12j,
                    1.09602131e-12+3.33066197e-12j,
                    1.25359248e-12+1.02630145e-12j])
    e3d = fields.get_receiver(efield, rec)

    # 10 % is still OK, grid is very coarse for fast comp (2s)
    assert_allclose(epm, e3d, rtol=0.1)


def test_get_magnetic_field():
    # Mainly regression tests, not ideal.

    # Check it does still the same (pure regression).
    dat = REGRES['reg_2']
    model = dat['model']
    efield = dat['result']
    hfield = dat['hresult']

    hout = fields.get_magnetic_field(model, efield)
    assert_allclose(hfield.field, hout.field)

    # Add some mu_r - Just 1, to trigger, and compare.
    dat = REGRES['res']
    efield = dat['Fresult']
    model1 = models.Model(**dat['input_model'])
    model2 = models.Model(**dat['input_model'], mu_r=1.)

    hout1 = fields.get_magnetic_field(model1, efield)
    hout2 = fields.get_magnetic_field(model2, efield)
    assert_allclose(hout1.field, hout2.field)

    # Ensure they are not the same if mu_r!=1/None provided
    model3 = models.Model(**dat['input_model'], mu_r=2.)
    hout3 = fields.get_magnetic_field(model3, efield)
    with pytest.raises(AssertionError):
        assert_allclose(hout1.field, hout3.field)


class TestFiniteSourceXYZ:

    def test_get_source_field_point_vs_finite(self, capsys):
        # === Point dipole to finite dipole comparisons ===
        def get_xyz(d_src):
            """Return dimensions corresponding to azimuth and dip."""
            h = np.cos(np.deg2rad(d_src[4]))
            dys = np.sin(np.deg2rad(d_src[3]))*h
            dxs = np.cos(np.deg2rad(d_src[3]))*h
            dzs = np.sin(np.deg2rad(d_src[4]))
            return [dxs, dys, dzs]

        def get_f_src(d_src, slen=1.0):
            """Return d_src and f_src for d_src input."""
            xyz = get_xyz(d_src)
            f_src = [d_src[0]-xyz[0]*slen/2, d_src[0]+xyz[0]*slen/2,
                     d_src[1]-xyz[1]*slen/2, d_src[1]+xyz[1]*slen/2,
                     d_src[2]-xyz[2]*slen/2, d_src[2]+xyz[2]*slen/2]
            return d_src, f_src

        # 1a. Source within one cell, normalized.
        h = np.ones(3)*500
        grid1 = meshes.TensorMesh([h, h, h], np.array([-750, -750, -750]))
        d_src, f_src = get_f_src([0, 0., 0., 23, 15])
        dsf = fields.get_source_field(grid1, d_src, 1)
        fsf = fields.get_source_field(grid1, f_src, 1)
        assert fsf == dsf

        # 1b. Source within one cell, source strength = pi.
        d_src, f_src = get_f_src([0, 0., 0., 32, 53])
        dsf = fields.get_source_field(grid1, d_src, 3.3, np.pi)
        fsf = fields.get_source_field(grid1, f_src, 3.3, np.pi)
        assert fsf == dsf

        # 1c. Source over various cells, normalized.
        h = np.ones(8)*200
        grid2 = meshes.TensorMesh([h, h, h], np.array([-800, -800, -800]))
        d_src, f_src = get_f_src([0, 0., 0., 40, 20], 300.0)
        dsf = fields.get_source_field(grid2, d_src, 10.0, 0)
        fsf = fields.get_source_field(grid2, f_src, 10.0, 0)
        assert_allclose(fsf.fx.sum(), dsf.fx.sum())
        assert_allclose(fsf.fy.sum(), dsf.fy.sum())
        assert_allclose(fsf.fz.sum(), dsf.fz.sum())

        # 1d. Source over various cells, source strength = pi.
        slen = 300
        strength = np.pi
        d_src, f_src = get_f_src([0, 0., 0., 20, 30], slen)
        dsf = fields.get_source_field(grid2, d_src, 1.3, slen*strength)
        fsf = fields.get_source_field(grid2, f_src, 1.3, strength)
        assert_allclose(fsf.fx.sum(), dsf.fx.sum())
        assert_allclose(fsf.fy.sum(), dsf.fy.sum())
        assert_allclose(fsf.fz.sum(), dsf.fz.sum())

        # 1e. Source over various stretched cells, source strength = pi.
        h1 = get_h(4, 2, 200, 1.1)
        h2 = get_h(4, 2, 200, 1.2)
        h3 = get_h(4, 2, 200, 1.2)
        origin = np.array([-h1.sum()/2, -h2.sum()/2, -h3.sum()/2])
        grid3 = meshes.TensorMesh([h1, h2, h3], origin)
        slen = 333
        strength = np.pi
        d_src, f_src = get_f_src([0, 0., 0., 50, 33], slen)
        dsf = fields.get_source_field(grid3, d_src, 0.7, slen*strength)
        fsf = fields.get_source_field(grid3, f_src, 0.7, strength)
        assert_allclose(fsf.fx.sum(), dsf.fx.sum())
        assert_allclose(fsf.fy.sum(), dsf.fy.sum())
        assert_allclose(fsf.fz.sum(), dsf.fz.sum())

    def test_source_norm_warning(self):
        # This is a warning that should never be raised...
        hx, x0 = np.ones(4), -2
        mesh = meshes.TensorMesh([hx, hx, hx], (x0, x0, x0))
        sfield = fields.Field(mesh, frequency=1)
        sfield.fx += 1  # Add something to the field.
        with pytest.warns(UserWarning, match="Normalizing Source: 101.000000"):
            _ = fields._finite_source_xyz(
                    mesh, (-0.5, 0.5, 0, 0, 0, 0), sfield.fx, 30)

    def test_warnings(self):
        h = np.ones(4)
        grid = meshes.TensorMesh([h*200, h*400, h*800], (-450, -850, -1650))
        # Put source way out. Ensure it fails.
        with pytest.raises(ValueError, match='Provided source outside grid'):
            _ = fields.get_source_field(grid, [1e10, 1e10, 1e10, 0, 0], 1)


def test_rotation():
    assert_allclose(fields._rotation(0, 0), [1, 0, 0])
    assert_allclose(fields._rotation(90, 0), [0, 1, 0])
    assert_allclose(fields._rotation(-90, 0), [0, -1, 0])
    assert_allclose(fields._rotation(0, 90), [0, 0, 1])
    assert_allclose(fields._rotation(0, -90), [0, 0, -1])
    dazm, ddip = 30, 60
    razm, rdip = np.deg2rad(dazm), np.deg2rad(ddip)
    assert_allclose(
            fields._rotation(dazm, ddip),
            [np.cos(razm)*np.cos(rdip), np.sin(razm)*np.cos(rdip),
             np.sin(rdip)])
    dazm, ddip = -45, 180
    razm, rdip = np.deg2rad(dazm), np.deg2rad(ddip)
    assert_allclose(
            fields._rotation(dazm, ddip),
            [np.cos(razm)*np.cos(rdip), np.sin(razm)*np.cos(rdip),
             np.sin(rdip)],
            atol=1e-14)


def test_finite_dipole_from_point():
    source = (10, 100, -1000, 0, 0)
    length = 111.0
    out = fields._finite_dipole_from_point(source, length)
    assert out.shape == (6, )
    assert out[0] == source[0]-length/2
    assert out[1] == source[0]+length/2
    assert out[2] == source[1]
    assert out[3] == source[1]
    assert out[4] == source[2]
    assert out[5] == source[2]

    source = (10, 100, -1000, 30, 60)
    length = 2.0
    out = fields._finite_dipole_from_point(source, length)
    assert_allclose(
        out,
        [9.5669873, 10.4330127, 99.75, 100.25, -1000.8660254, -999.1339746]
    )


def test_square_loop_from_point():
    source = (10, 100, -1000, 0, 0)
    length = np.sqrt(2)
    out = fields._square_loop_from_point(source, length)
    assert out.shape == (3, 5)
    assert_allclose(out[0, :], source[0])  # x-directed, all x the same
    assert_allclose(out[1, :], [101, 100, 99, 100, 101])
    assert_allclose(out[2, :], [-1000, -999, -1000, -1001, -1000])

    source = (10, 100, -1000, 30, 60)
    length = np.sqrt(2)
    out = fields._square_loop_from_point(source, length)
    assert_allclose(out[0, :], [9.5, 9.25, 10.5, 10.75, 9.5])
    assert_allclose(out[2, :], [-1000, -999.5, -1000, -1000.5, -1000])
    assert_allclose(out[:, 0], out[:, -1])  # first and last point identical
