Changelog
#########


*latest* : Flexible grid size
-----------------------------

- **First open-source version**, including the Travis-RTD-Coveralls-Codacy
  toolchain and Zenodo. No benchmarks yet.
- Accepts now *any* grid size (warns if a bad grid size for MG is provided).
- Coarsens now to the lowest level of each dimension, not only to the coarsest
  level of the smallest dimension.
- Combined ``restrict_rx``, ``restrict_ry``, and ``restrict_rz`` to
  ``restrict``.
- Improve speed by passing pre-allocated arrays to jitted functions.
- Store ``res_y``, ``res_z`` and corresponding ``eta_y``, ``eta_z`` only if
  ``res_y``, ``res_z`` were provided in initial call to ``utils.model``.
- Change ``zeta`` to ``v_mu_r``.
- Include rudimentary ``TensorMesh``-class in ``utils``; removes hard
  dependency on ``discretize``.
- Bugfix: Take a provided ``efield`` into account; don't return if provided.


v0.4.0 : Cholesky
-----------------

**2019-03-29**

- Use ``solve_chol`` for everything, remove ``solve_zlin``.
- Moved ``mesh.py`` and some functionalities from ``solver.py`` into
  ``utils.py``.
- New mesh-tools. Should move to ``discretize`` eventually.
- Improved source generation tool. Might also move to ``discretize``.
- ``printversion`` is now included in ``utils``.
- Many bug fixes.
- Lots of improvements to tests.
- Lots of improvements to documentation. Amongst other, moved docs from
  ``__init__.py`` into the docs rst.


v0.3.0 : Semicoarsening
-----------------------

**2019-01-18**

- Semicoarsening option.
- Number of cells must still be 2^n, but n can be different in the x-, y-, and
  z-directions.
- Many other iterative solvers from :mod:`scipy.sparse.linalg` can be used. It
  seems to work fine with the following methods:

  - :func:`scipy.sparse.linalg.bicgstab`:  BIConjugate Gradient STABilize;
  - :func:`scipy.sparse.linalg.cgs`: Conjugate Gradient Squared;
  - :func:`scipy.sparse.linalg.gmres`: Generalized Minimal RESidual;
  - :func:`scipy.sparse.linalg.lgmres`: Improvement of GMRES using alternating
    residual vectors;
  - :func:`scipy.sparse.linalg.gcrotmk`: GCROT: Generalized Conjugate Residual
    with inner Orthogonalization and Outer Truncation.

- The SciPy-solver or MG can be used all in combination or on its own, hence
  only MG, SciPy-solver with MG preconditioning, only SciPy-solver.


v0.2.0 : Line relaxation
------------------------

**2019-01-14**

- Line relaxation option.


v0.1.0 : Initial
----------------

**2018-12-28**

- Standard multigrid with or without BiCGSTAB.
- Tri-axial anisotropy.
- Number of cells must be 2^n, and n has to be the same in the x-, y-, and
  z-directions.