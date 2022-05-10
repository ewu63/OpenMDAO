"""Define the LinearBlockGS class."""

import numpy as np

from openmdao.solvers.solver import BlockLinearSolver
from openmdao.devtools.debug import dprint, get_indent


class LinearBlockGS(BlockLinearSolver):
    """
    Linear block Gauss-Seidel solver.

    Parameters
    ----------
    **kwargs : dict
        Options dictionary.

    Attributes
    ----------
    _delta_d_n_1 : ndarray
        Cached change in the d_output vectors for the previous iteration. Only used if the
        aitken acceleration option is turned on.
    _theta_n_1 : float
        Cached relaxation factor from previous iteration. Only used if the aitken acceleration
        option is turned on.
    """

    SOLVER = 'LN: LNBGS'

    def __init__(self, **kwargs):
        """
        Initialize all attributes.
        """
        super().__init__(**kwargs)

        self._theta_n_1 = None
        self._delta_d_n_1 = None

    def _declare_options(self):
        """
        Declare options before kwargs are processed in the init method.
        """
        super()._declare_options()

        self.options.declare('use_aitken', types=bool, default=False,
                             desc='set to True to use Aitken relaxation')
        self.options.declare('aitken_min_factor', default=0.1,
                             desc='lower limit for Aitken relaxation factor')
        self.options.declare('aitken_max_factor', default=1.5,
                             desc='upper limit for Aitken relaxation factor')
        self.options.declare('aitken_initial_factor', default=1.0,
                             desc='initial value for Aitken relaxation factor')

    def _setup_solvers(self, system, depth):
        """
        Assign system instance, set depth, and optionally perform setup.

        Parameters
        ----------
        system : <System>
            pointer to the owning system.
        depth : int
            depth of the current system (already incremented).
        """
        super()._setup_solvers(system, depth)
        topsol = system._problem_meta['top_LNBGS']
        self._matfree_cache_comps = []
        if topsol is None:  # I'm the top LNBGS solver
            from openmdao.core.component import Component
            for s in system.system_iter(recurse=True, typ=Component):
                if s.options['matrix_free_caching']:
                    self._matfree_cache_comps.append(s)
            system._problem_meta['top_LNBGS'] = system.pathname

    def _iter_initialize(self):
        """
        Perform any necessary pre-processing operations.

        Returns
        -------
        float
            initial error.
        float
            error at the first iteration.
        """
        if self.options['use_aitken']:
            if self._mode == 'fwd':
                self._delta_d_n_1 = self._system()._vectors['output']['linear'].asarray(copy=True)
            else:
                self._delta_d_n_1 = self._system()._vectors['residual']['linear'].asarray(copy=True)
            self._theta_n_1 = 1.0

        return super()._iter_initialize()

    def _single_iteration(self):
        """
        Perform the operations in the iteration loop.
        """
        system = self._system()
        mode = self._mode
        use_aitken = self.options['use_aitken']

        if use_aitken:
            aitken_min_factor = self.options['aitken_min_factor']
            aitken_max_factor = self.options['aitken_max_factor']

            # some variables that are used for Aitken's relaxation
            delta_d_n_1 = self._delta_d_n_1
            theta_n_1 = self._theta_n_1

            # store a copy of the outputs, used to compute the change in outputs later
            if self._mode == 'fwd':
                d_out_vec = system._vectors['output']['linear']
            else:
                d_out_vec = system._vectors['residual']['linear']

            d_n = d_out_vec.asarray(copy=True)
            delta_d_n = d_out_vec.asarray(copy=True)

        if mode == 'fwd':
            # b_vec = system._vectors['residual']['linear']
            par_off = system._vectors['residual']['linear']._root_offset

            for subsys, _ in system._subsystems_allprocs.values():
                if self._rel_systems is not None and subsys.pathname not in self._rel_systems:
                    continue
                # must always do the transfer on all procs even if subsys not local
                system._transfer('linear', mode, subsys.name)

                if not subsys._is_local:
                    continue

                b_vec = subsys._vectors['residual']['linear']
                scope_out, scope_in = system._get_matvec_scope(subsys)
                subsys._apply_linear(None, self._rel_systems, mode, scope_out, scope_in)

                b_vec *= -1.0
                off = b_vec._root_offset - par_off
                b_vec += self._rhs_vec[off:off + len(b_vec)]
                subsys._solve_linear(mode, self._rel_systems)

        else:  # rev
            subsystems = list(system._subsystems_allprocs)
            subsystems.reverse()
            # b_vec = system._vectors['output']['linear']
            par_off = system._vectors['output']['linear']._root_offset
            for s in self._matfree_cache_comps:
                s._reset_lin_hashes()  # we're the highest level LNBGS. reset hashes for each iter

            for sname in subsystems:
                subsys, _ = system._subsystems_allprocs[sname]

                if self._rel_systems is not None and subsys.pathname not in self._rel_systems:
                    continue

                if subsys._is_local:
                    b_vec = subsys._vectors['output']['linear']
                    dprint(get_indent(self), f"LNBGS (sub '{subsys.pathname}) ZERO doutputs")
                    b_vec.set_val(0.0)

                    system._transfer('linear', mode, sname)
                    # dprint(get_indent(self), "transfer to doutputs of", sname, 'vec=',
                    #     subsys._vectors['output']['linear'].asarray())

                    b_vec *= -1.0
                    # dprint(get_indent(self), "add rhs_vec to -doutputs")
                    off = b_vec._root_offset - par_off
                    b_vec += self._rhs_vec[off:off + len(b_vec)]
                    # dprint(get_indent(self), "doutputs =", b_vec.asarray())

                    subsys._solve_linear(mode, self._rel_systems)
                    scope_out, scope_in = system._get_matvec_scope(subsys)

                    subsys._apply_linear(None, self._rel_systems, mode, scope_out, scope_in)
                else:   # subsys not local
                    system._transfer('linear', mode, sname)

        if use_aitken:
            if self._mode == 'fwd':
                d_resid_vec = system._vectors['residual']['linear']
                d_out_vec = system._vectors['output']['linear']
            else:
                d_resid_vec = system._vectors['output']['linear']
                d_out_vec = system._vectors['residual']['linear']

            theta_n = self.options['aitken_initial_factor']

            # compute the change in the outputs after the NLBGS iteration
            delta_d_n -= d_out_vec.asarray()
            delta_d_n *= -1

            if self._iter_count >= 2:
                # Compute relaxation factor. This method is used by Kenway et al. in
                # "Scalable Parallel Approach for High-Fidelity Steady-State Aero-
                # elastic Analysis and Adjoint Derivative Computations" (ln 22 of Algo 1)

                temp = delta_d_n.copy()
                temp -= delta_d_n_1

                # If MPI, piggyback on the residual vector to perform a distributed norm.
                if system.comm.size > 1:
                    backup_r = d_resid_vec.asarray(copy=True)
                    d_resid_vec.set_val(temp)
                    temp_norm = d_resid_vec.get_norm()
                else:
                    temp_norm = np.linalg.norm(temp)

                if temp_norm == 0.:
                    temp_norm = 1e-12  # prevent division by 0 below

                # If MPI, piggyback on the output and residual vectors to perform a distributed
                # dot product.
                if system.comm.size > 1:
                    backup_o = d_out_vec.asarray(copy=True)
                    d_out_vec.set_val(delta_d_n)
                    tddo = d_resid_vec.dot(d_out_vec)
                    d_resid_vec.set_val(backup_r)
                    d_out_vec.set_val(backup_o)
                else:
                    tddo = temp.dot(delta_d_n)

                theta_n = theta_n_1 * (1 - tddo / temp_norm ** 2)

            else:
                # keep the initial the relaxation factor
                pass

            # limit relaxation factor to the specified range
            theta_n = max(aitken_min_factor, min(aitken_max_factor, theta_n))

            # save relaxation factor for the next iteration
            self._theta_n_1 = theta_n

            d_out_vec.set_val(d_n)

            # compute relaxed outputs
            d_out_vec += theta_n * delta_d_n

            # save update to use in next iteration
            delta_d_n_1[:] = delta_d_n
