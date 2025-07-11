"""Implementation of algorithm for sparse multi-subjects learning of Gaussian \
graphical models.
"""

import collections.abc
import itertools
import operator
import warnings

import numpy as np
import scipy.linalg
from joblib import Memory, Parallel, delayed
from sklearn.base import BaseEstimator
from sklearn.covariance import empirical_covariance
from sklearn.model_selection import check_cv
from sklearn.utils import check_array
from sklearn.utils.extmath import fast_logdet

from nilearn._utils import CacheMixin, fill_doc, logger
from nilearn._utils.extmath import is_spd
from nilearn._utils.logger import find_stack_level
from nilearn._utils.param_validation import check_params
from nilearn._utils.tags import SKLEARN_LT_1_6


def compute_alpha_max(emp_covs, n_samples):
    """Compute the critical value of the regularization parameter.

    Above this value, the precisions matrices computed by
    group_sparse_covariance are diagonal (complete sparsity)

    This function also returns the value below which the precision
    matrices are fully dense (i.e. minimal number of zero coefficients).

    The formula used in this function was derived using the same method
    as in :footcite:t:`Duchi2012`.

    Parameters
    ----------
    emp_covs : array-like, shape (n_features, n_features, n_subjects)
        covariance matrix for each subject.

    n_samples : array-like, shape (n_subjects,)
        number of samples used in the computation of every covariance matrix.
        n_samples.sum() can be arbitrary.

    Returns
    -------
    alpha_max : float
        minimal value for the regularization parameter that gives a
        fully sparse matrix.

    alpha_min : float
        minimal value for the regularization parameter that gives a fully
        dense matrix.

    References
    ----------
    .. footbibliography::

    """
    A = np.copy(emp_covs)
    n_samples = np.asarray(n_samples).copy()
    n_samples /= n_samples.sum()

    for k in range(emp_covs.shape[-1]):
        # Set diagonal to zero
        A[..., k].flat[:: A.shape[0] + 1] = 0
        A[..., k] *= n_samples[k]

    norms = np.sqrt((A**2).sum(axis=-1))

    return np.max(norms), np.min(norms[norms > 0])


def _update_submatrix(full, sub, sub_inv, p, h, v):
    """Update submatrix and its inverse.

    sub_inv is the inverse of the submatrix of "full" obtained by removing
    the p-th row and column.

    sub_inv is modified in-place. After execution of this function, it contains
    the inverse of the submatrix of "full" obtained by removing the n+1-th row
    and column.

    This computation is based on the Sherman-Woodbury-Morrison identity.

    """
    n = p - 1
    v[: n + 1] = full[: n + 1, n]
    v[n + 1 :] = full[n + 2 :, n]
    h[: n + 1] = full[n, : n + 1]
    h[n + 1 :] = full[n, n + 2 :]

    # change row: first usage of SWM identity
    coln = sub_inv[:, n : n + 1]  # 2d array, useful for sub_inv below
    V = h - sub[n, :]
    coln = coln / (1.0 + np.dot(V, coln))
    # The following line is equivalent to
    # sub_inv -= np.outer(coln, np.dot(V, sub_inv))
    sub_inv -= np.dot(coln, np.dot(V, sub_inv)[np.newaxis, :])
    sub[n, :] = h

    # change column: second usage of SWM identity
    rown = sub_inv[n : n + 1, :]  # 2d array, useful for sub_inv below
    U = v - sub[:, n]
    rown = rown / (1.0 + np.dot(rown, U))
    # The following line is equivalent to (but faster)
    # sub_inv -= np.outer(np.dot(sub_inv, U), rown)
    sub_inv -= np.dot(np.dot(sub_inv, U)[:, np.newaxis], rown)
    sub[:, n] = v  # equivalent to sub[n, :] += U

    # Make sub_inv symmetric (overcome some numerical limitations)
    sub_inv += sub_inv.T.copy()
    sub_inv /= 2.0


def _assert_submatrix(full, sub, n):
    """Check that "sub" is the matrix obtained \
    by removing the p-th col and row in "full".

    Used only for debugging.

    """
    true_sub = np.empty_like(sub)
    true_sub[:n, :n] = full[:n, :n]
    true_sub[n:, n:] = full[n + 1 :, n + 1 :]
    true_sub[:n, n:] = full[:n, n + 1 :]
    true_sub[n:, :n] = full[n + 1 :, :n]

    np.testing.assert_almost_equal(true_sub, sub)


@fill_doc
def group_sparse_covariance(
    subjects,
    alpha,
    max_iter=50,
    tol=1e-3,
    verbose=0,
    probe_function=None,
    precisions_init=None,
    debug=False,
):
    """Compute sparse precision matrices and covariance matrices.

    The precision matrices returned by this function are sparse, and share a
    common sparsity pattern: all have zeros at the same location. This is
    achieved by simultaneous computation of all precision matrices at the
    same time.

    Running time is linear on max_iter, and number of subjects (len(subjects)),
    but cubic on number of features (subjects[0].shape[1]).

    The present algorithm is based on :footcite:t:`Honorio2012`.

    Parameters
    ----------
    subjects : :obj:`list` of numpy.ndarray
        input subjects. Each subject is a 2D array, whose columns contain
        signals. Each array shape must be (sample number, feature number).
        The sample number can vary from subject to subject, but all subjects
        must have the same number of features (i.e. of columns).

    alpha : :obj:`float`
        regularization parameter. With normalized covariances matrices and
        number of samples, sensible values lie in the [0, 1] range(zero is
        no regularization: output is not sparse)

    max_iter : :obj:`int`, default=50
        maximum number of iterations.

    tol : positive :obj:`float` or None, default=0.001
        The tolerance to declare convergence: if the duality gap goes below
        this value, optimization is stopped. If None, no check is performed.

    %(verbose0)s

    probe_function : callable or None,  default=None
        This value is called before the first iteration and after each
        iteration. If it returns True, then optimization is stopped
        prematurely.
        The function is given as arguments (in that order):

        - empirical covariances (ndarray),
        - number of samples for each subject (ndarray),
        - regularization parameter (float)
        - maximum iteration number (integer)
        - tolerance (float)
        - current iteration number (integer). -1 means "before first iteration"
        - current value of precisions (ndarray).
        - previous value of precisions (ndarray). None before first iteration.

    precisions_init : numpy.ndarray,  default=None
        initial value of the precision matrices. If not provided, a diagonal
        matrix with the variances of each input signal is used.

    debug : :obj:`bool`, default=False
        if True, perform checks during computation. It can help find
        numerical problems, but increases computation time a lot.

    Returns
    -------
    emp_covs : numpy.ndarray, shape (n_features, n_features, n_subjects)
        empirical covariances matrices

    precisions : numpy.ndarray, shape (n_features, n_features, n_subjects)
        estimated precision matrices

    References
    ----------
    .. footbibliography::

    """
    emp_covs, n_samples = empirical_covariances(
        subjects, assume_centered=False
    )

    precisions = _group_sparse_covariance(
        emp_covs,
        n_samples,
        alpha,
        max_iter=max_iter,
        tol=tol,
        verbose=verbose,
        precisions_init=precisions_init,
        probe_function=probe_function,
        debug=debug,
    )

    return emp_covs, precisions


def _group_sparse_covariance(
    emp_covs,
    n_samples,
    alpha,
    max_iter=10,
    tol=1e-3,
    precisions_init=None,
    probe_function=None,
    verbose=0,
    debug=False,
):
    """Implement an internal version of group_sparse_covariance.

    See its docstring for details.

    """
    if tol == -1:
        tol = None

    _check_alpha(alpha)

    n_subjects = emp_covs.shape[-1]
    n_features = emp_covs[0].shape[0]
    n_samples = np.asarray(n_samples)
    n_samples /= n_samples.sum()  # essential for numerical stability

    _check_diagonal_normalization(emp_covs, n_subjects)

    omega = _init_omega(emp_covs, precisions_init)

    # Preallocate arrays
    y = np.ndarray(shape=(n_subjects, n_features - 1), dtype=np.float64)
    u = np.ndarray(shape=(n_subjects, n_features - 1), dtype=np.float64)
    y_1 = np.ndarray(shape=(n_subjects, n_features - 2), dtype=np.float64)
    h_12 = np.ndarray(shape=(n_subjects, n_features - 2), dtype=np.float64)
    q = np.ndarray(shape=(n_subjects,), dtype=np.float64)
    aq = np.ndarray(shape=(n_subjects,), dtype=np.float64)  # temp. array
    c = np.ndarray(shape=(n_subjects,), dtype=np.float64)
    W = np.ndarray(
        shape=(omega.shape[0] - 1, omega.shape[1] - 1, omega.shape[2]),
        dtype=np.float64,
        order="F",
    )
    W_inv = np.ndarray(shape=W.shape, dtype=np.float64, order="F")

    # Auxiliary arrays.
    v = np.ndarray((omega.shape[0] - 1,), dtype=np.float64)
    h = np.ndarray((omega.shape[1] - 1,), dtype=np.float64)

    # Optional.
    tolerance_reached = False
    max_norm = None

    omega_old = np.empty_like(omega)

    if probe_function is not None:
        # iteration number -1 means called before iteration loop.
        probe_function(
            emp_covs, n_samples, alpha, max_iter, tol, -1, omega, None
        )

    probe_interrupted = False

    # Start optimization loop. Variables are named following (mostly) the
    # Honorio-Samaras paper notations.

    # Used in the innermost loop. Computed here to save some computation.
    alpha2 = alpha**2

    for n in range(max_iter):
        suffix = (
            f" variation (max norm): {max_norm:.3e} "
            if max_norm is not None
            else ""
        )

        logger.log(
            f"* iteration {n:d} ({100.0 * n / max_iter:.0f} %){suffix} ...",
            verbose=verbose,
        )

        omega_old[...] = omega
        for p in range(n_features):
            if p == 0:
                W, W_inv = _set_initial_state_w_and_w_inv(omega, debug, p)

            else:
                if debug:
                    omega_orig = omega.copy()

                _update_w_and_w_inv(
                    omega, debug, W, W_inv, n_subjects, p, h, v
                )

                if debug:
                    # Check that omega has not been modified.
                    np.testing.assert_almost_equal(omega_orig, omega)

            # In the following lines, implicit loop on k (subjects)
            # Extract y and u
            y[:, :p] = omega[:p, p, :].T
            y[:, p:] = omega[p + 1 :, p, :].T

            u[:, :p] = emp_covs[:p, p, :].T
            u[:, p:] = emp_covs[p + 1 :, p, :].T

            for m in range(n_features - 1):
                # Coordinate descent on y

                # T(k) -> n_samples[k]
                # v(k) -> emp_covs[p, p, k]
                # h_22(k) -> W_inv[m, m, k]
                # h_12(k) -> W_inv[:m, m, k],  W_inv[m+1:, m, k]
                # y_1(k) -> y[k, :m], y[k, m+1:]
                # u_2(k) -> u[k, m]
                h_12[:, :m] = W_inv[:m, m, :].T
                h_12[:, m:] = W_inv[m + 1 :, m, :].T
                y_1[:, :m] = y[:, :m]
                y_1[:, m:] = y[:, m + 1 :]

                c[:] = -n_samples * (
                    emp_covs[p, p, :] * (h_12 * y_1).sum(axis=1) + u[:, m]
                )
                c2 = np.sqrt(np.dot(c, c))

                # x -> y[:][m]
                if c2 <= alpha:
                    y[:, m] = 0  # x* = 0
                else:
                    # q(k) -> T(k) * v(k) * h_22(k)
                    # \lambda -> gamma   (lambda is a Python keyword)
                    q[:] = n_samples * emp_covs[p, p, :] * W_inv[m, m, :]

                    if debug:
                        assert np.all(q > 0)
                    # x* = \lambda* diag(1 + \lambda q)^{-1} c

                    # Newton-Raphson loop. Loosely based on Scipy's.
                    # Tolerance does not seem to be important for numerical
                    # stability (tolerance of 1e-2 works) but has an effect on
                    # overall convergence rate (the tighter the better.)

                    gamma = 0.0  # initial value
                    # Precompute some quantities
                    cc = c * c
                    two_ccq = 2.0 * cc * q
                    for _ in itertools.repeat(None, 100):
                        # Function whose zero must be determined (fval) and
                        # its derivative (fder).
                        # Written inplace to save some function calls.
                        aq = 1.0 + gamma * q
                        aq2 = aq * aq
                        fder = (two_ccq / (aq2 * aq)).sum()

                        if fder == 0:
                            msg = "derivative was zero."
                            warnings.warn(
                                msg,
                                RuntimeWarning,
                                stacklevel=find_stack_level(),
                            )
                            break
                        fval = -(alpha2 - (cc / aq2).sum()) / fder
                        gamma = fval + gamma
                        if abs(fval) < 1.5e-8:
                            break

                    if abs(fval) > 0.1:
                        warnings.warn(
                            "Newton-Raphson step did not converge.\n"
                            "This may indicate a badly conditioned system.",
                            stacklevel=find_stack_level(),
                        )

                    if debug:
                        assert gamma >= 0.0, gamma

                    y[:, m] = (gamma * c) / aq  # x*

            # Copy back y in omega (column and row)
            omega[:p, p, :] = y[:, :p].T
            omega[p + 1 :, p, :] = y[:, p:].T
            omega[p, :p, :] = y[:, :p].T
            omega[p, p + 1 :, :] = y[:, p:].T

            for k in range(n_subjects):
                omega[p, p, k] = 1.0 / emp_covs[p, p, k] + np.dot(
                    np.dot(y[k, :], W_inv[..., k]), y[k, :]
                )

                if debug:
                    assert is_spd(omega[..., k])

        if probe_function is not None and probe_function(
            emp_covs,
            n_samples,
            alpha,
            max_iter,
            tol,
            n,
            omega,
            omega_old,
        ):
            probe_interrupted = True
            logger.log(
                "probe_function interrupted loop", verbose=verbose, msg_level=2
            )
            break

        # Compute max of variation
        omega_old -= omega
        omega_old = abs(omega_old)
        max_norm = omega_old.max()

        tolerance_reached = _check_if_tolerance_reached(
            tol, max_norm, verbose, n
        )
        if tolerance_reached:
            break

    if tol is not None and not tolerance_reached and not probe_interrupted:
        warnings.warn(
            "Maximum number of iterations reached without getting "
            "to the requested tolerance level.",
            stacklevel=find_stack_level(),
        )

    return omega


def _init_omega(emp_covs, precisions_init):
    """Initialize omega value."""
    if precisions_init is None:
        n_subjects = emp_covs.shape[-1]
        # Fortran order make omega[..., k] contiguous, which is often useful.
        omega = np.ndarray(shape=emp_covs.shape, dtype=np.float64, order="F")
        for k in range(n_subjects):
            # Values on main diagonals are far from zero, because they
            # are timeseries energy.
            omega[..., k] = np.diag(1.0 / np.diag(emp_covs[..., k]))
    else:
        omega = precisions_init.copy()

    return omega


def _check_alpha(alpha):
    if not isinstance(alpha, (int, float)) or alpha < 0:
        raise ValueError(
            "Regularization parameter alpha must be a positive number.\n"
            f"You provided: {alpha=}"
        )


def _check_diagonal_normalization(emp_covs, n_subjects):
    ones = np.ones(emp_covs.shape[0])
    for k in range(n_subjects):
        if (
            abs(emp_covs[..., k].flat[:: emp_covs.shape[0] + 1] - ones) > 0.1
        ).any():
            warnings.warn(
                "Input signals do not all have unit variance. "
                "This can lead to numerical instability.",
                stacklevel=find_stack_level(),
            )
            break


def _set_initial_state_w_and_w_inv(omega, debug, p):
    """Set initial state by removing first col/row."""
    W = omega[1:, 1:, :].copy()  # stack of W(k)
    W_inv = np.ndarray(shape=W.shape, dtype=np.float64)
    for k in range(W.shape[2]):
        # stack of W^-1(k)
        W_inv[..., k] = scipy.linalg.inv(W[..., k])

        if debug:
            np.testing.assert_almost_equal(
                np.dot(W_inv[..., k], W[..., k]),
                np.eye(W_inv[..., k].shape[0]),
                decimal=10,
            )
            _assert_submatrix(omega[..., k], W[..., k], p)
            assert is_spd(W_inv[..., k])

    return W, W_inv


def _update_w_and_w_inv(omega, debug, W, W_inv, n_subjects, p, h, v):
    for k in range(n_subjects):
        _update_submatrix(omega[..., k], W[..., k], W_inv[..., k], p, h, v)

        if debug:
            _assert_submatrix(omega[..., k], W[..., k], p)
            assert is_spd(W_inv[..., k], decimal=14)
            np.testing.assert_almost_equal(
                np.dot(W[..., k], W_inv[..., k]),
                np.eye(W_inv[..., k].shape[0]),
                decimal=10,
            )


def _check_if_tolerance_reached(tol, max_norm, verbose, n):
    tolerance_reached = tol is not None and max_norm < tol
    if tolerance_reached:
        logger.log(
            f"tolerance reached at iteration number {n + 1:d}: {max_norm:.3e}",
            verbose=verbose,
        )
    return tolerance_reached


@fill_doc
class GroupSparseCovariance(CacheMixin, BaseEstimator):
    """Covariance and precision matrix estimator.

    The model used has been introduced in :footcite:t:`Varoquaux2010a`, and the
    algorithm used is based on what is described in :footcite:t:`Honorio2012`.

    Parameters
    ----------
    alpha : :obj:`float`, default=0.1
        regularization parameter. With normalized covariances matrices and
        number of samples, sensible values lie in the [0, 1] range(zero is
        no regularization: output is not sparse).

    tol : positive :obj:`float`, default=1e-3
        The tolerance to declare convergence: if the dual gap goes below
        this value, iterations are stopped.

    max_iter : :obj:`int`, default=10
        maximum number of iterations. The default value is rather
        conservative.

    %(verbose0)s

    %(memory)s

    %(memory_level)s

    Attributes
    ----------
    covariances_ : numpy.ndarray, shape (n_features, n_features, n_subjects)
        empirical covariance matrices.

    precisions_ : numpy.ndarraye, shape (n_features, n_features, n_subjects)
        precisions matrices estimated using the group-sparse algorithm.

    References
    ----------
    .. footbibliography::

    """

    def __init__(
        self,
        alpha=0.1,
        tol=1e-3,
        max_iter=10,
        verbose=0,
        memory=None,
        memory_level=0,
    ):
        self.alpha = alpha
        self.tol = tol
        self.max_iter = max_iter

        self.memory = memory
        self.memory_level = memory_level
        self.verbose = verbose

    def _more_tags(self):
        """Return estimator tags.

        TODO remove when bumping sklearn_version > 1.5
        """
        return self.__sklearn_tags__()

    def __sklearn_tags__(self):
        """Return estimator tags.

        See the sklearn documentation for more details on tags
        https://scikit-learn.org/1.6/developers/develop.html#estimator-tags
        """
        if SKLEARN_LT_1_6:
            from nilearn._utils.tags import tags

            return tags(niimg_like=False)

        from nilearn._utils.tags import InputTags

        tags = super().__sklearn_tags__()
        tags.input_tags = InputTags(niimg_like=False)
        return tags

    @fill_doc
    def fit(self, subjects, y=None):
        """Fits the group sparse precision model according \
        to the given training data and parameters.

        Parameters
        ----------
        subjects : :obj:`list` of numpy.ndarray \
                   with shapes (n_samples, n_features)
            input subjects. Each subject is a 2D array, whose columns contain
            signals. Sample number can vary from subject to subject, but all
            subjects must have the same number of features (i.e. of columns).

        %(y_dummy)s

        Returns
        -------
        self : GroupSparseCovariance instance
            the object itself. Useful for chaining operations.

        """
        del y
        check_params(self.__dict__)

        # casting single arrays to list mostly to help
        # with checking comlpliance with sklearn estimator guidelines
        if isinstance(subjects, np.ndarray):
            subjects = [subjects]

        if not isinstance(subjects, list):
            raise TypeError(
                "'subjects' must be a list of arrays. "
                f"Got {subjects.__class__.__name__}"
            )

        for x in subjects:
            check_array(
                x,
                accept_sparse=False,
                ensure_2d=True,
                ensure_min_features=2,
                ensure_min_samples=2,
            )

        if self.memory is None:
            self.memory = Memory(location=None)

        logger.log("Computing covariance matrices", verbose=self.verbose)
        self.covariances_, n_samples = empirical_covariances(
            subjects, assume_centered=False
        )

        self.n_features_in_ = next(iter(s.shape[1] for s in subjects))

        logger.log("Computing precision matrices", verbose=self.verbose)
        ret = self._cache(_group_sparse_covariance)(
            self.covariances_,
            n_samples,
            self.alpha,
            tol=self.tol,
            max_iter=self.max_iter,
            verbose=max(0, self.verbose - 1),
            debug=False,
        )

        self.precisions_ = ret
        return self

    def __sklearn_is_fitted__(self):
        return hasattr(self, "precisions_") and hasattr(self, "covariances_")


def empirical_covariances(subjects, assume_centered=False, standardize=False):
    """Compute empirical covariances for several signals.

    Parameters
    ----------
    subjects : :obj:`list` of numpy.ndarray, \
        shape for each (n_samples, n_features)
        input subjects. Each subject is a 2D array, whose columns contain
        signals. Sample number can vary from subject to subject, but all
        subjects must have the same number of features (i.e. of columns).

    assume_centered : :obj:`bool`, default=False
        if True, assume that all input signals are centered. This slightly
        decreases computation time by avoiding useless computation.

    standardize : :obj:`bool`, default=False
        if True, set every signal variance to one before computing their
        covariance matrix (i.e. compute a correlation matrix).

    Returns
    -------
    emp_covs : numpy.ndarray, \
        shape : (feature number, feature number, subject number)
        empirical covariances.

    n_samples : numpy.ndarray, shape: (subject number,)
        number of samples for each subject. dtype is np.float64.

    """
    if not hasattr(subjects, "__iter__"):
        raise ValueError(
            "'subjects' input argument must be an iterable. "
            f"You provided {subjects.__class__}"
        )

    n_subjects = [s.shape[1] for s in subjects]
    if len(set(n_subjects)) > 1:
        raise ValueError(
            "All subjects must have the same number of "
            f"features.\nYou provided: {n_subjects}"
        )
    n_subjects = len(subjects)
    n_features = subjects[0].shape[1]

    # Enable to change dtype here because depending on user, conversion from
    # single precision to double will be required or not.
    emp_covs = np.empty((n_features, n_features, n_subjects), order="F")
    for k, s in enumerate(subjects):
        if standardize:
            s = s / s.std(axis=0)  # copy on purpose
        M = empirical_covariance(s, assume_centered=assume_centered)

        # Force matrix symmetry, for numerical stability
        # of _group_sparse_covariance
        emp_covs[..., k] = M + M.T
    emp_covs /= 2

    n_samples = np.asarray([s.shape[0] for s in subjects], dtype=np.float64)

    return emp_covs, n_samples


def group_sparse_scores(
    precisions, n_samples, emp_covs, alpha, duality_gap=False, debug=False
):
    """Compute scores used by group_sparse_covariance.

    The log-likelihood of a given list of empirical covariances /
    precisions.

    Parameters
    ----------
    precisions : numpy.ndarray, shape (n_features, n_features, n_subjects)
        estimated precisions.

    n_samples : array-like, shape (n_subjects,)
        number of samples used in estimating each subject in "precisions".
        n_samples.sum() must be equal to 1.

    emp_covs : numpy.ndarray, shape (n_features, n_features, n_subjects)
        empirical covariance matrix

    alpha : :obj:`float`
        regularization parameter

    duality_gap : :obj:`bool`, default=False
        if True, also returns a duality gap upper bound.

    debug : :obj:`bool`, default=False
        if True, some consistency checks are performed to help solving
        numerical problems.

    Returns
    -------
    log_lik : float
        log-likelihood of precisions on the given covariances. This is the
        opposite of the loss function, without the regularization term

    objective : float
        value of objective function. This is the value minimized by
        group_sparse_covariance()

    duality_gap : float
        duality gap upper bound. The returned bound is tight: it vanishes for
        the optimal precision matrices

    """
    n_features, _, n_subjects = emp_covs.shape

    log_lik = 0
    for k in range(n_subjects):
        log_lik_k = -np.sum(emp_covs[..., k] * precisions[..., k])
        log_lik_k += fast_logdet(precisions[..., k])
        log_lik += n_samples[k] * log_lik_k

    l2 = np.sqrt((precisions**2).sum(axis=-1))
    l12 = l2.sum() - np.diag(l2).sum()  # Do not count diagonal terms
    objective = alpha * l12 - log_lik
    ret = (log_lik, objective)

    # Compute duality gap if requested
    if duality_gap is True:
        A = np.empty(precisions.shape, dtype=np.float64, order="F")
        for k in range(n_subjects):
            # TODO: can be computed more efficiently using W_inv. See
            # Friedman, Jerome, Trevor Hastie, and Robert Tibshirani.
            # 'Sparse Inverse Covariance Estimation with the Graphical Lasso'.
            # Biostatistics 9, no. 3 (1 July 2008): 432-441.
            precisions_inv = scipy.linalg.inv(precisions[..., k])
            if debug:
                assert is_spd(precisions_inv)

            A[..., k] = n_samples[k] * (precisions_inv - emp_covs[..., k])

            if debug:
                np.testing.assert_almost_equal(A[..., k], A[..., k].T)

        # Project A on the set of feasible points
        alpha_max = np.sqrt((A**2).sum(axis=-1))
        mask = alpha_max > alpha
        for k in range(A.shape[-1]):
            A[mask, k] *= alpha / alpha_max[mask]
            # Set zeros on diagonals. Essential to get an always positive
            # duality gap.
            A[..., k].flat[:: A.shape[0] + 1] = 0

        dual_obj = 0  # dual objective
        for k in range(n_subjects):
            B = emp_covs[..., k] + A[..., k] / n_samples[k]
            dual_obj += n_samples[k] * (n_features + fast_logdet(B))

        # The previous computation can lead to a non-feasible point, because
        # one of the Bs may not be positive definite.
        # Use another value in this case, that ensure positive definiteness
        # of B. The upper bound on the duality gap is not tight in the
        # following, but is smaller than infinity, which is better in any case.
        if not np.isfinite(dual_obj):
            for k in range(n_subjects):
                A[..., k] = -n_samples[k] * emp_covs[..., k]
                A[..., k].flat[:: A.shape[0] + 1] = 0
            alpha_max = np.sqrt((A**2).sum(axis=-1)).max()
            # the second value (0.05 is arbitrary: positive in ]0,1[)
            gamma = min((alpha / alpha_max, 0.05))
            dual_obj = 0
            for k in range(n_subjects):
                # add gamma on the diagonal
                B = (1.0 - gamma) * emp_covs[..., k] + gamma * np.eye(
                    emp_covs.shape[0]
                )
                dual_obj += n_samples[k] * (n_features + fast_logdet(B))

        gap = objective - dual_obj
        ret = (*ret, gap)
    return ret


@fill_doc
def group_sparse_covariance_path(
    train_subjs,
    alphas,
    test_subjs=None,
    tol=1e-3,
    max_iter=10,
    precisions_init=None,
    verbose=0,
    debug=False,
    probe_function=None,
):
    """Get estimated precision matrices for different values of alpha.

    Calling this function is faster than calling group_sparse_covariance()
    repeatedly, because it makes use of the first result to initialize the
    next computation.

    Parameters
    ----------
    train_subjs : :obj:`list` of numpy.ndarray
        list of signals.

    alphas : :obj:`list` of :obj:`float`
         values of alpha to use. Best results for sorted values (decreasing)

    test_subjs : :obj:`list` of numpy.ndarray, default=None
        list of signals, independent from those in train_subjs, on which to
        compute a score. If None, no score is computed.

    %(verbose0)s

    tol, max_iter, debug, precisions_init :
        Passed to group_sparse_covariance(). See the corresponding docstring
        for details.

    probe_function : callable, default=None
        This value is called before the first iteration and after each
        iteration. If it returns True, then optimization is stopped
        prematurely.
        The function is given as arguments (in that order):

        - empirical covariances (ndarray),
        - number of samples for each subject (ndarray),
        - regularization parameter (float)
        - maximum iteration number (integer)
        - tolerance (float)
        - current iteration number (integer). -1 means "before first iteration"
        - current value of precisions (ndarray).
        - previous value of precisions (ndarray). None before first iteration.

    Returns
    -------
    precisions_list : :obj:`list` of numpy.ndarray
        estimated precisions for each value of alpha provided. The length of
        this list is the same as that of parameter "alphas".

    scores : :obj:`list` of float
        for each estimated precision, score obtained on the test set. Output
        only if test_subjs is not None.

    """
    train_covs, train_n_samples = empirical_covariances(
        train_subjs, assume_centered=False, standardize=True
    )

    scores = []
    precisions_list = []
    for alpha in alphas:
        precisions = _group_sparse_covariance(
            train_covs,
            train_n_samples,
            alpha,
            tol=tol,
            max_iter=max_iter,
            precisions_init=precisions_init,
            verbose=max(0, verbose - 1),
            debug=debug,
            probe_function=probe_function,
        )

        # Compute log-likelihood
        if test_subjs is not None:
            test_covs, _ = empirical_covariances(
                test_subjs, assume_centered=False, standardize=True
            )
            scores.append(
                group_sparse_scores(precisions, train_n_samples, test_covs, 0)[
                    0
                ]
            )
        precisions_list.append(precisions)
        precisions_init = precisions

    return (
        (precisions_list, scores)
        if test_subjs is not None
        else precisions_list
    )


class EarlyStopProbe:
    """Callable probe for early stopping in GroupSparseCovarianceCV.

    Stop optimizing as soon as the score on the test set starts decreasing.
    An instance of this class is supposed to be passed in the probe_function
    argument of group_sparse_covariance().

    """

    def __init__(self, test_subjs, verbose=0):
        self.test_emp_covs, _ = empirical_covariances(test_subjs)
        self.verbose = verbose

    def __call__(  # noqa: D102
        self,
        emp_covs,  # noqa: ARG002
        n_samples,
        alpha,
        max_iter,  # noqa: ARG002
        tol,  # noqa: ARG002
        iter_n,
        omega,
        prev_omega,  # noqa: ARG002
    ):
        log_lik, _ = group_sparse_scores(
            omega, n_samples, self.test_emp_covs, alpha
        )
        if iter_n > -1 and self.last_log_lik > log_lik:
            logger.log(
                "Log-likelihood on test set is decreasing. "
                f"Stopping at iteration {iter_n}",
                verbose=self.verbose,
            )
            return True
        self.last_log_lik = log_lik


@fill_doc
class GroupSparseCovarianceCV(BaseEstimator):
    """Sparse inverse covariance w/ cross-validated choice of the parameter.

    A cross-validated value for the regularization parameter is first
    determined using several calls to group_sparse_covariance. Then a final
    optimization is run to get a value for the precision matrices, using the
    selected value of the parameter. Different values of tolerance and of
    maximum iteration number can be used in these two phases (see the tol
    and tol_cv keyword below for example).

    Parameters
    ----------
    alphas : :obj:`int`, default=4
        initial number of points in the grid of regularization parameter
        values. Each step of grid refinement adds that many points as well.

    n_refinements : :obj:`int`, default=4
        number of times the initial grid should be refined.

    cv : :obj:`int`, default=None
        number of folds in a K-fold cross-validation scheme.

    tol_cv : :obj:`float`, default=1e-2
        tolerance used to get the optimal alpha value. It has the same meaning
        as the `tol` parameter in :func:`group_sparse_covariance`.

    max_iter_cv : :obj:`int`, default=50
        maximum number of iterations for each optimization, during the alpha-
        selection phase.

    tol : :obj:`float`, default=1e-3
        tolerance used during the final optimization for determining precision
        matrices value.

    max_iter : :obj:`int`, default=100
        maximum number of iterations in the final optimization.

    %(verbose0)s

    %(n_jobs)s

    debug : :obj:`bool`, default=False
        if True, activates some internal checks for consistency. Only useful
        for nilearn developers, not users.

    early_stopping : :obj:`bool`, default=True
        if True, reduce computation time by using a heuristic to reduce the
        number of iterations required to get the optimal value for alpha. Be
        aware that this can lead to slightly different values for the optimal
        alpha compared to early_stopping=False.

    Attributes
    ----------
    covariances_ : numpy.ndarray, shape (n_features, n_features, n_subjects)
        covariance matrices, one per subject.

    precisions_ : numpy.ndarray, shape (n_features, n_features, n_subjects)
        precision matrices, one per subject. All matrices have the same
        sparsity pattern (if a coefficient is zero for a given matrix, it
        is also zero for every other.)

    alpha_ : float
        penalization parameter value selected.

    cv_alphas_ : list of floats
        all values of the penalization parameter explored.

    cv_scores_ : numpy.ndarray, shape (n_alphas, n_folds)
        scores obtained on test set for each value of the penalization
        parameter explored.

    See Also
    --------
    GroupSparseCovariance,
    sklearn.covariance.GraphicalLassoCV

    Notes
    -----
    The search for the optimal penalization parameter (alpha) is done on an
    iteratively refined grid: first the cross-validated scores on a grid are
    computed, then a new refined grid is centered around the maximum, and so
    on.

    """

    def __init__(
        self,
        alphas=4,
        n_refinements=4,
        cv=None,
        tol_cv=1e-2,
        max_iter_cv=50,
        tol=1e-3,
        max_iter=100,
        verbose=0,
        n_jobs=1,
        debug=False,
        early_stopping=True,
    ):
        self.alphas = alphas
        self.n_refinements = n_refinements
        self.tol_cv = tol_cv
        self.max_iter_cv = max_iter_cv
        self.cv = cv
        self.tol = tol
        self.max_iter = max_iter

        self.verbose = verbose
        self.n_jobs = n_jobs
        self.debug = debug
        self.early_stopping = early_stopping

    def _more_tags(self):
        """Return estimator tags.

        TODO remove when bumping sklearn_version > 1.5
        """
        return self.__sklearn_tags__()

    def __sklearn_tags__(self):
        """Return estimator tags.

        See the sklearn documentation for more details on tags
        https://scikit-learn.org/1.6/developers/develop.html#estimator-tags
        """
        if SKLEARN_LT_1_6:
            from nilearn._utils.tags import tags

            return tags(niimg_like=False)

        from nilearn._utils.tags import InputTags

        tags = super().__sklearn_tags__()
        tags.input_tags = InputTags(niimg_like=False)
        return tags

    @fill_doc
    def fit(self, subjects, y=None):
        """Compute cross-validated group-sparse precisions.

        Parameters
        ----------
        subjects : :obj:`list` of numpy.ndarray \
            with shapes (n_samples, n_features)
            input subjects. Each subject is a 2D array, whose columns contain
            signals. Sample number can vary from subject to subject, but all
            subjects must have the same number of features (i.e. of columns.)

        %(y_dummy)s

        Returns
        -------
        self : GroupSparseCovarianceCV
            the object instance itself.

        """
        del y
        check_params(self.__dict__)

        # casting single arrays to list mostly to help
        # with checking comlpliance with sklearn estimator guidelines
        if isinstance(subjects, np.ndarray):
            subjects = [subjects]

        if not isinstance(subjects, list):
            raise TypeError(
                "'subjects' must be a list of 2D numpy arrays. "
                f"Got {subjects.__class__.__name__}"
            )

        for x in subjects:
            check_array(
                x,
                accept_sparse=False,
                ensure_2d=True,
                ensure_min_features=2,
                ensure_min_samples=2,
            )

        # Empirical covariances
        emp_covs, n_samples = empirical_covariances(
            subjects, assume_centered=False
        )
        n_subjects = emp_covs.shape[2]

        self.n_features_in_ = next(iter(s.shape[1] for s in subjects))

        # One cv generator per subject must be created, because each subject
        # can have a different number of samples from the others.
        cv = [
            check_cv(
                self.cv, np.ones(subjects[k].shape[0]), classifier=False
            ).split(subjects[k])
            for k in range(n_subjects)
        ]
        path = []  # List of (alpha, scores, covs)
        n_alphas = self.alphas

        if isinstance(n_alphas, collections.abc.Sequence):
            alphas = list(self.alphas)
            n_refinements = 1
        else:
            n_refinements = self.n_refinements
            alpha_1, _ = compute_alpha_max(emp_covs, n_samples)
            alpha_0 = 1e-2 * alpha_1
            alphas = np.logspace(
                np.log10(alpha_0), np.log10(alpha_1), n_alphas
            )[::-1]

        covs_init = itertools.repeat(None)

        # Copying the cv generators to use them n_refinements times.
        cv_ = zip(*cv)

        for i, (this_cv) in enumerate(itertools.tee(cv_, n_refinements)):
            # Compute the cross-validated loss on the current grid
            train_test_subjs = []
            for train_test in this_cv:
                assert len(train_test) == n_subjects
                train_test_subjs.append(
                    list(
                        zip(
                            *[
                                (subject[train, :], subject[test, :])
                                for subject, (train, test) in zip(
                                    subjects, train_test
                                )
                            ]
                        )
                    )
                )
            if self.early_stopping:
                probes = [
                    EarlyStopProbe(
                        test_subjs, verbose=max(0, self.verbose - 1)
                    )
                    for _, test_subjs in train_test_subjs
                ]
            else:
                probes = itertools.repeat(None)

            this_path = Parallel(n_jobs=self.n_jobs, verbose=self.verbose)(
                delayed(group_sparse_covariance_path)(
                    train_subjs,
                    alphas,
                    test_subjs=test_subjs,
                    max_iter=self.max_iter_cv,
                    tol=self.tol_cv,
                    verbose=max(0, self.verbose - 1),
                    debug=self.debug,
                    # Warm restart is useless with early stopping.
                    precisions_init=None if self.early_stopping else prec_init,
                    probe_function=probe,
                )
                for (train_subjs, test_subjs), prec_init, probe in zip(
                    train_test_subjs, covs_init, probes
                )
            )

            # this_path[i] is a tuple (precisions_list, scores)
            # - scores: scores obtained with the i-th folding, for each value
            #   of alpha.
            # - precisions_list: corresponding precisions matrices, for each
            #   value of alpha.
            precisions_list, scores = list(zip(*this_path))
            # now scores[i][j] is the score for the i-th folding, j-th value of
            # alpha (analogous for precisions_list)
            precisions_list = list(zip(*precisions_list))
            scores = [np.mean(sc) for sc in zip(*scores)]
            # scores[i] is the mean score obtained for the i-th value of alpha.

            path.extend(list(zip(alphas, scores, precisions_list)))
            path = sorted(path, key=operator.itemgetter(0), reverse=True)

            # Find the maximum score (avoid using the built-in 'max' function
            # to have a fully-reproducible selection of the smallest alpha in
            # case of equality)
            best_score = -np.inf
            last_finite_idx = 0
            for index, (_, this_score, _) in enumerate(path):
                if this_score >= 0.1 / np.finfo(np.float64).eps:
                    this_score = np.nan
                if np.isfinite(this_score):
                    last_finite_idx = index
                if this_score >= best_score:
                    best_score = this_score
                    best_index = index

            # Refine the grid
            if best_index == 0:
                # We do not need to go back: we have chosen
                # the highest value of alpha for which there are
                # non-zero coefficients
                alpha_1 = path[0][0]
                alpha_0 = path[1][0]
                covs_init = path[0][2]
            elif best_index == last_finite_idx and best_index != len(path) - 1:
                # We have non-converged models on the upper bound of the
                # grid, we need to refine the grid there
                alpha_1 = path[best_index][0]
                alpha_0 = path[best_index + 1][0]
                covs_init = path[best_index][2]
            elif best_index == len(path) - 1:
                alpha_1 = path[best_index][0]
                alpha_0 = 0.01 * path[best_index][0]
                covs_init = path[best_index][2]
            else:
                alpha_1 = path[best_index - 1][0]
                alpha_0 = path[best_index + 1][0]
                covs_init = path[best_index - 1][2]
            alphas = np.logspace(
                np.log10(alpha_1), np.log10(alpha_0), len(alphas) + 2
            )
            alphas = alphas[1:-1]
            if n_refinements > 1:
                logger.log(
                    "[GroupSparseCovarianceCV] Done refinement "
                    f"{i: 2} out of {n_refinements}",
                    verbose=self.verbose,
                )

        path = list(zip(*path))
        cv_scores_ = list(path[1])
        alphas = list(path[0])

        self.cv_scores_ = np.array(cv_scores_)
        self.alpha_ = alphas[best_index]
        self.cv_alphas_ = alphas

        # Finally, fit the model with the selected alpha
        logger.log("Final optimization", verbose=self.verbose)
        self.covariances_ = emp_covs
        self.precisions_ = _group_sparse_covariance(
            emp_covs,
            n_samples,
            self.alpha_,
            tol=self.tol,
            max_iter=self.max_iter,
            verbose=max(0, self.verbose - 1),
            debug=self.debug,
        )
        return self

    def __sklearn_is_fitted__(self):
        return hasattr(self, "precisions_") and hasattr(self, "covariances_")
