"""
Electromagnetic modeller in the diffusive limit (low frequencies) for 3D media
with tri-axial electrical anisotropy. The matrix-free multigrid solver can be
used as main solver or as preconditioner for one of the Krylov subspace methods
implemented in :mod:`scipy.sparse.linalg`, and the governing equations are
discretized on a staggered Yee grid. The code is written completely in Python
using the ``numpy``/``scipy``-stack, where the most time-consuming parts are
sped-up through jitted ``numba``-functions.
"""
# Copyright 2018-2019 The emg3d Developers.
#
# This file is part of emg3d.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License.  You may obtain a copy
# of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  See the
# License for the specific language governing permissions and limitations under
# the License.

from . import utils
from . import solver
from .utils import Report

__all__ = ['solver', 'utils', 'Report']

# Version
try:
    # - Released versions just tags:       0.8.0
    # - GitHub commits add .dev#+hash:     0.8.1.dev4+g2785721
    # - Uncommitted changes add timestamp: 0.8.1.dev4+g2785721.d20191022
    from .version import version as __version__
except ImportError:
    # If it was not installed, then we don't know the version. We could throw a
    # warning here, but this case *should* be rare. emg3d should be installed
    # properly!
    __version__ = 'unknown-'+utils.datetime.today().strftime('%Y%m%d')
