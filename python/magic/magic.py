"""
Markov Affinity-based Graph Imputation of Cells (MAGIC)

Authors:
Scott Gigante <scott.gigante@yale.edu>, Daniel Dager <daniel.dager@yale.edu>
(C) 2018 Krishnaswamy Lab GPLv2
"""

from __future__ import print_function, division, absolute_import

import numpy as np
import graphtools
from sklearn.base import BaseEstimator
from sklearn.exceptions import NotFittedError
from sklearn.decomposition import PCA
import warnings
import matplotlib.pyplot as plt
from scipy import sparse, spatial
import pandas as pd
import numbers
import tasklogger
import scprep

from . import utils

try:
    import anndata
except ImportError:
    # anndata not installed
    pass


class MAGIC(BaseEstimator):
    """MAGIC operator which performs dimensionality reduction.

    Markov Affinity-based Graph Imputation of Cells (MAGIC) is an
    algorithm for denoising and transcript recover of single cells
    applied to single-cell RNA sequencing data, as described in
    van Dijk et al, 2018 [1]_.

    Parameters
    ----------

    k : int, optional, default: 10
        number of nearest neighbors on which to build kernel

    a : int, optional, default: 15
        sets decay rate of kernel tails.
        If None, alpha decaying kernel is not used

    t : int, optional, default: 'auto'
        power to which the diffusion operator is powered.
        This sets the level of diffusion. If 'auto', t is selected
        according to the Procrustes disparity of the diffused data

    n_pca : int, optional, default: 100
        Number of principal components to use for calculating
        neighborhoods. For extremely large datasets, using
        n_pca < 20 allows neighborhoods to be calculated in
        roughly log(n_samples) time.

    knn_dist : string, optional, default: 'euclidean'
        recommended values: 'euclidean', 'cosine'
        Any metric from `scipy.spatial.distance` can be used
        distance metric for building kNN graph.

    n_jobs : integer, optional, default: 1
        The number of jobs to use for the computation.
        If -1 all CPUs are used. If 1 is given, no parallel computing code is
        used at all, which is useful for debugging.
        For n_jobs below -1, (n_cpus + 1 + n_jobs) are used. Thus for
        n_jobs = -2, all CPUs but one are used

    random_state : integer or numpy.RandomState, optional, default: None
        The generator used to initialize random PCA
        If an integer is given, it fixes the seed
        Defaults to the global `numpy` random number generator

    verbose : `int` or `boolean`, optional (default: 1)
        If `True` or `> 0`, print status messages

    Attributes
    ----------

    X : array-like, shape=[n_samples, n_features]
        Input data

    X_magic : array-like, shape=[n_samples, n_features]
        Output data

    graph : graphtools.BaseGraph
        The graph built on the input data

    Examples
    --------
    >>> import magic
    >>> import pandas as pd
    >>> import matplotlib.pyplot as plt
    >>> X = pd.read_csv("../../data/test_data.csv")
    >>> X.shape
    (500, 197)
    >>> magic_operator = magic.MAGIC()
    >>> X_magic = magic_operator.fit_transform(X, genes=['VIM', 'CDH1', 'ZEB1'])
    >>> X_magic.shape
    (500, 3)
    >>> magic_operator.set_params(t=7)
    MAGIC(a=15, k=5, knn_dist='euclidean', n_jobs=1, n_pca=100,
       random_state=None, t=7, verbose=1)
    >>> X_magic = magic_operator.transform(genes=['VIM', 'CDH1', 'ZEB1'])
    >>> X_magic.shape
    (500, 3)
    >>> X_magic = magic_operator.transform(genes="all_genes")
    >>> X_magic.shape
    (500, 197)
    >>> plt.scatter(X_magic['VIM'], X_magic['CDH1'],
    ...             c=X_magic['ZEB1'], s=1, cmap='inferno')
    >>> plt.show()
    >>> magic.plot.animate_magic(X, gene_x='VIM', gene_y='CDH1',
    ...                          gene_color='ZEB1', operator=magic_operator)
    >>> dremi = magic_operator.knnDREMI('VIM', 'CDH1', plot=True)

    References
    ----------
    .. [1] Van Dijk D *et al.* (2018),
        *Recovering Gene Interactions from Single-Cell Data Using Data Diffusion*,
        `Cell <https://www.cell.com/cell/abstract/S0092-8674(18)30724-4>`__.
    """

    def __init__(self, k=10, a=15, t='auto', n_pca=100,
                 knn_dist='euclidean', n_jobs=1, random_state=None,
                 verbose=1):
        self.k = k
        self.a = a
        self.t = t
        self.n_pca = n_pca
        self.knn_dist = knn_dist
        self.n_jobs = n_jobs
        self.random_state = random_state

        self.graph = None
        self.X = None
        self.X_magic = None
        self._check_params()
        self.verbose = verbose
        tasklogger.set_level(verbose)

    @property
    def diff_op(self):
        """The diffusion operator calculated from the data
        """
        if self.graph is not None:
            return self.graph.diff_op
        else:
            raise NotFittedError("This MAGIC instance is not fitted yet. Call "
                                 "'fit' with appropriate arguments before "
                                 "using this method.")

    def _check_params(self):
        """Check MAGIC parameters

        This allows us to fail early - otherwise certain unacceptable
        parameter choices, such as mds='mmds', would only fail after
        minutes of runtime.

        Raises
        ------
        ValueError : unacceptable choice of parameters
        """
        utils.check_positive(k=self.k)
        utils.check_int(k=self.k,
                        n_jobs=self.n_jobs)
        # TODO: epsilon
        utils.check_between(v_min=0,
                            v_max=100)
        utils.check_if_not(None, utils.check_positive, utils.check_int,
                           n_pca=self.n_pca)
        utils.check_if_not(None, utils.check_positive,
                           a=self.a)
        utils.check_if_not('auto', utils.check_positive, utils.check_int,
                           t=self.t)
        utils.check_in(['euclidean', 'cosine', 'correlation',
                        'cityblock', 'l1', 'l2', 'manhattan', 'braycurtis',
                        'canberra', 'chebyshev', 'dice', 'hamming', 'jaccard',
                        'kulsinski', 'mahalanobis', 'matching', 'minkowski',
                        'rogerstanimoto', 'russellrao', 'seuclidean',
                        'sokalmichener', 'sokalsneath', 'sqeuclidean', 'yule'],
                       knn_dist=self.knn_dist)

    def _set_graph_params(self, **params):
        try:
            self.graph.set_params(**params)
        except AttributeError:
            # graph not defined
            pass

    def set_params(self, **params):
        """Set the parameters on this estimator.

        Any parameters not given as named arguments will be left at their
        current value.

        Parameters
        ----------

        k : int, optional, default: 10
            number of nearest neighbors on which to build kernel

        a : int, optional, default: 15
            sets decay rate of kernel tails.
            If None, alpha decaying kernel is not used

        t : int, optional, default: 'auto'
            power to which the diffusion operator is powered.
            This sets the level of diffusion. If 'auto', t is selected
            according to the R squared of the diffused data

        n_pca : int, optional, default: 100
            Number of principal components to use for calculating
            neighborhoods. For extremely large datasets, using
            n_pca < 20 allows neighborhoods to be calculated in
            roughly log(n_samples) time.

        knn_dist : string, optional, default: 'euclidean'
            recommended values: 'euclidean', 'cosine'
            Any metric from `scipy.spatial.distance` can be used
            distance metric for building kNN graph.

        n_jobs : integer, optional, default: 1
            The number of jobs to use for the computation.
            If -1 all CPUs are used. If 1 is given, no parallel computing code
            is used at all, which is useful for debugging.
            For n_jobs below -1, (n_cpus + 1 + n_jobs) are used. Thus for
            n_jobs = -2, all CPUs but one are used

        random_state : integer or numpy.RandomState, optional, default: None
            The generator used to initialize random PCA
            If an integer is given, it fixes the seed
            Defaults to the global `numpy` random number generator

        verbose : `int` or `boolean`, optional (default: 1)
            If `True` or `> 0`, print status messages

        Returns
        -------
        self
        """
        reset_kernel = False
        reset_imputation = False
        # diff potential parameters
        if 't' in params and params['t'] != self.t:
            self.t = params['t']
            reset_imputation = True
            del params['t']

        # kernel parameters
        if 'k' in params and params['k'] != self.k:
            self.k = params['k']
            reset_kernel = True
            del params['k']
        if 'a' in params and params['a'] != self.a:
            self.a = params['a']
            reset_kernel = True
            del params['a']
        if 'n_pca' in params and params['n_pca'] != self.n_pca:
            self.n_pca = params['n_pca']
            reset_kernel = True
            del params['n_pca']
        if 'knn_dist' in params and params['knn_dist'] != self.knn_dist:
            self.knn_dist = params['knn_dist']
            reset_kernel = True
            del params['knn_dist']

        # parameters that don't change the embedding
        if 'n_jobs' in params:
            self.n_jobs = params['n_jobs']
            self._set_graph_params(n_jobs=params['n_jobs'])
            del params['n_jobs']
        if 'random_state' in params:
            self.random_state = params['random_state']
            self._set_graph_params(random_state=params['random_state'])
            del params['random_state']
        if 'verbose' in params:
            self.verbose = params['verbose']
            tasklogger.set_level(self.verbose)
            self._set_graph_params(verbose=params['verbose'])
            del params['verbose']

        if reset_kernel:
            # can't reset the graph kernel without making a new graph
            self.graph = None
            reset_imputation = True
        if reset_imputation:
            self.X_magic = None

        self._check_params()
        return self

    def fit(self, X, graph=None):
        """Computes the diffusion operator

        Parameters
        ----------
        X : array, shape=[n_samples, n_features]
            input data with `n_samples` samples and `n_features`
            dimensions. Accepted data types: `numpy.ndarray`,
            `scipy.sparse.spmatrix`, `pd.DataFrame`, `anndata.AnnData`.
        graph : `graphtools.Graph`, optional (default: None)
            If given, provides a precomputed kernel matrix with which to
            perform diffusion.

        Returns
        -------
        magic_operator : MAGIC
            The estimator object
        """
        if self.n_pca is None or X.shape[1] <= self.n_pca:
            n_pca = None
        else:
            n_pca = self.n_pca

        if graph is None:
            graph = self.graph
            if self.X is not None and not \
                    utils.matrix_is_equivalent(X, self.X):
                """
                If the same data is used, we can reuse existing kernel and
                diffusion matrices. Otherwise we have to recompute.
                """
                tasklogger.log_debug(
                    "Reset graph due to difference in input data")
                graph = None
            elif graph is not None:
                try:
                    graph.set_params(
                        decay=self.a, knn=self.k + 1, distance=self.knn_dist,
                        n_jobs=self.n_jobs, verbose=self.verbose, n_pca=n_pca,
                        thresh=1e-4, random_state=self.random_state)
                except ValueError as e:
                    # something changed that should have invalidated the graph
                    tasklogger.log_debug(
                        "Reset graph due to {}".format(str(e)))
                    graph = None
        else:
            self.k = graph.knn - 1
            self.alpha = graph.decay
            self.n_pca = graph.n_pca
            self.knn_dist = graph.distance

        self.X = X

        if utils.has_empty_columns(X):
            warnings.warn("Input matrix contains unexpressed genes. "
                          "Please remove them prior to running MAGIC.")

        if graph is not None:
            tasklogger.log_info(
                "Using precomputed graph and diffusion operator...")
            self.graph = graph
        else:
            # reset X_magic in case it was previously set
            self.X_magic = None
            tasklogger.log_start("graph and diffusion operator")
            self.graph = graphtools.Graph(
                X,
                n_pca=n_pca,
                knn=self.k + 1,
                decay=self.a,
                thresh=1e-4,
                n_jobs=self.n_jobs,
                verbose=self.verbose,
                random_state=self.random_state)
            tasklogger.log_complete("graph and diffusion operator")

        return self

    def transform(self, X=None, genes=None, t_max=20,
                  plot_optimal_t=False, ax=None):
        """Computes the values of genes after diffusion

        Parameters
        ----------
        X : array, optional, shape=[n_samples, n_features]
            input data with `n_samples` samples and `n_features`
            dimensions. Not required, since MAGIC does not embed
            cells not given in the input matrix to `MAGIC.fit()`.
            Accepted data types: `numpy.ndarray`,
            `scipy.sparse.spmatrix`, `pd.DataFrame`, `anndata.AnnData`.

        genes : list or {"all_genes", "pca_only"}, optional (default: None)
            List of genes, either as integer indices or column names
            if input data is a pandas DataFrame. If "all_genes", the entire
            smoothed matrix is returned. If "pca_only", PCA on the smoothed
            data is returned. If None, the entire matrix is also
            returned, but a warning may be raised if the resultant matrix
            is very large.

        t_max : int, optional, default: 20
            maximum t to test if `t` is set to 'auto'

        plot_optimal_t : boolean, optional, default: False
            If true and `t` is set to 'auto', plot the disparity used to
            select t

        ax : matplotlib.axes.Axes, optional
            If given and `plot_optimal_t` is true, plot will be drawn
            on the given axis.

        Returns
        -------
        X_magic : array, shape=[n_samples, n_genes]
            The gene expression values after diffusion
        """
        try:
            if isinstance(X, anndata.AnnData):
                if (genes is None or (isinstance(genes, str)
                                      and genes in ['all_genes', 'pca_only'])):
                    # special names
                    pass
                else:
                    # ensure the genes is a 1D ndarray
                    genes = np.array([genes]).flatten()
                    if issubclass(genes.dtype.type, numbers.Integral):
                        # integer indices
                        pass
                    else:
                        # names
                        genes = np.argwhere(np.isin(X.var_names,
                                                    genes)).flatten()
        except NameError:
            # anndata not installed
            pass

        if self.graph is None:
            if self.X is not None:
                self.fit(self.X)
            else:
                raise NotFittedError(
                    "This MAGIC instance is not fitted yet. Call "
                    "'fit' with appropriate arguments before "
                    "using this method.")

        store_result = True
        if X is not None and not utils.matrix_is_equivalent(X, self.X):
            store_result = False
            graph = graphtools.base.Data(X, n_pca=self.n_pca)
            warnings.warn(UserWarning, "Running MAGIC.transform on different "
                          "data to that which was used for MAGIC.fit may not "
                          "produce sensible output, unless it comes from the "
                          "same manifold.")
        else:
            X = self.X
            graph = self.graph
            store_result = True

        if genes is None and isinstance(X, (pd.SparseDataFrame,
                                            sparse.spmatrix)) and \
                np.prod(X.shape) > 5000 * 20000:
            warnings.warn("Returning imputed values for all genes on a ({} x "
                          "{}) matrix will require approximately {}GB of "
                          "memory. Suppress this warning with "
                          "`genes='all_genes'`".format(
                              X.shape[0], X.shape[1],
                              np.prod(X.shape) * 8 / (1024**3)),
                          UserWarning)
        if isinstance(genes, str) and genes == "all_genes":
            genes = None
        elif isinstance(genes, str) and genes == "pca_only":
            if not hasattr(self.graph, "data_pca"):
                raise RuntimeError("Cannot return PCA as PCA is not"
                                   " performed.")
        elif genes is not None:
            genes = np.array([genes]).flatten()
            if not issubclass(genes.dtype.type, numbers.Integral):
                # gene names
                if isinstance(X, pd.DataFrame):
                    gene_names = X.columns
                elif utils.is_anndata(X):
                    gene_names = X.var_names
                else:
                    raise ValueError(
                        "Non-integer gene names only valid with pd.DataFrame "
                        "or anndata.AnnData input. "
                        "X is a {}, genes = {}".format(type(X).__name__,
                                                       genes))
                if not np.all(np.isin(genes, gene_names)):
                    warnings.warn("genes {} missing from input data".format(
                        genes[~np.isin(genes, gene_names)]))
                genes = np.argwhere(np.isin(gene_names, genes)).reshape(-1)

        if store_result and self.X_magic is not None:
            X_magic = self.X_magic
        else:
            X_magic = self._impute(graph, t_max=t_max,
                                   plot=plot_optimal_t, ax=ax)
            if store_result:
                self.X_magic = X_magic

        # return selected genes
        if isinstance(genes, str) and genes == "pca_only":
            X_magic = PCA().fit_transform(X_magic)
            genes = ["PC{}".format(i + 1) for i in range(X_magic.shape[1])]
        else:
            X_magic = graph.inverse_transform(X_magic, columns=genes)
            # convert back to pandas dataframe, if necessary
        X_magic = utils.convert_to_same_format(X_magic, X, columns=genes,
                                               prevent_sparse=True)
        return X_magic

    def fit_transform(self, X, graph=None, **kwargs):
        """Computes the diffusion operator and the position of the cells in the
        embedding space

        Parameters
        ----------
        X : array, shape=[n_samples, n_features]
            input data with `n_samples` samples and `n_features`
            dimensions. Accepted data types: `numpy.ndarray`,
            `scipy.sparse.spmatrix`, `pd.DataFrame`, `anndata.AnnData`.

        graph : `graphtools.Graph`, optional (default: None)
            If given, provides a precomputed kernel matrix with which to
            perform diffusion.

        kwargs : further arguments for `PHATE.transform()`
            Keyword arguments as specified in :func:`~phate.PHATE.transform`

        Returns
        -------
        X_magic : array, shape=[n_samples, n_genes]
            The gene expression values after diffusion
        """
        tasklogger.log_start('MAGIC')
        self.fit(X, graph=graph)
        X_magic = self.transform(**kwargs)
        tasklogger.log_complete('MAGIC')
        return X_magic

    def _calculate_error(self, data, data_prev=None, weights=None,
                         subsample_genes=None):
        """Calculates difference before and after diffusion

        Parameters
        ----------
        data : array-like
            current data matrix
        data_prev : array-like, optional (default: None)
            previous data matrix. If None, `data` is simply prepared for
            comparison and no error is returned
        weights : list-like, optional (default: None)
            weightings for dimensions of data. If None, dimensions are equally
            weighted
        subsample_genes : like-like, optional (default: None)
            genes to select in subsampling. If None, no subsampling is
            performed

        Returns
        -------
        error : float
            Procrustes disparity value
        data_curr : array-like
            transformed data to use for the next comparison
        """
        if subsample_genes is not None:
            data = data[:, subsample_genes]
        if weights is None:
            weights = np.ones(data.shape[1]) / data.shape[1]
        if data_prev is not None:
            _, _, error = spatial.procrustes(data_prev, data)
        else:
            error = None
        return error, data

    def _impute(self, data, t_max=20, plot=False, ax=None,
                max_genes_compute_t=500, threshold=0.001):
        """Peform MAGIC imputation

        Parameters
        ----------
        data : graphtools.Graph, graphtools.Data or array-like
            Input data
        t_max : int, optional (default: 20)
            Maximum value of t to consider for optimal t selection
        plot : bool, optional (default: False)
            Plot the optimal t selection graph
        ax : matplotlib.Axes, optional (default: None)
            Axis on which to plot. If None, a new axis is created
        max_genes_compute_t : int, optional (default: 500)
            Above this number, genes will be subsampled for
            optimal t selection
        threshold : float, optional (default: 0.001)
            Threshold after which Procrustes disparity is considered
            to have converged for optimal t selection

        Returns
        -------
        X_magic : array-like, shape=[n_samples, n_pca]
            Imputed data
        """
        if not isinstance(data, graphtools.base.Data):
            data = graphtools.base.Data(data, n_pca=self.n_pca)
        data_imputed = data.data_nu

        if data_imputed.shape[1] > max_genes_compute_t:
            subsample_genes = np.random.choice(data_imputed.shape[1],
                                               max_genes_compute_t,
                                               replace=False)
        else:
            subsample_genes = None
        if hasattr(data, "data_pca"):
            weights = None  # data.data_pca.explained_variance_ratio_
        else:
            weights = None
        if self.t == 'auto':
            _, data_prev = self._calculate_error(
                data_imputed, data_prev=None,
                weights=weights,
                subsample_genes=subsample_genes)
            error_vec = []
            t_opt = None
        else:
            t_opt = self.t

        tasklogger.log_start("imputation")

        # classic magic
        # the diffusion matrix is powered when t has been specified by
        # the user, and the dimensions of the diffusion matrix are lesser
        # than those of the data matrix. (M^t) * D
        if (t_opt is not None) and \
                (self.diff_op.shape[1] < data_imputed.shape[1]):
            diff_op_t = np.linalg.matrix_power(self.diff_op, t_opt)
            data_imputed = diff_op_t.dot(data_imputed)

        # fast magic
        # a while loop is used when the dimensions of the diffusion matrix
        # are greater than those of the data matrix, or when t is not specified
        # (so as to allow for the calculation of the optimal t value)
        else:
            i = 0
            while (t_opt is None and i < t_max) or \
                    (t_opt is not None and i < t_opt):
                i += 1
                data_imputed = self.diff_op.dot(data_imputed)
                if self.t == 'auto':
                    error, data_prev = self._calculate_error(
                        data_imputed, data_prev,
                        weights=weights,
                        subsample_genes=subsample_genes)
                    error_vec.append(error)
                    tasklogger.log_debug("{}: {}".format(i, error_vec))
                    if error < threshold and t_opt is None:
                        t_opt = i + 1
                        tasklogger.log_info(
                            "Automatically selected t = {}".format(t_opt))

        tasklogger.log_complete("imputation")

        if plot:
            # continue to t_max
            tasklogger.log_start("optimal t plot")
            if t_opt is None:
                # never converged
                warnings.warn("optimal t > t_max ({})".format(t_max),
                              RuntimeWarning)
            else:
                data_overimputed = data_imputed
                while i < t_max:
                    i += 1
                    data_overimputed = self.diff_op.dot(data_overimputed)
                    error, data_prev = self._calculate_error(
                        data_overimputed, data_prev,
                        weights=weights,
                        subsample_genes=subsample_genes)
                    error_vec.append(error)

            # create axis
            if ax is None:
                fig, ax = plt.subplots()
                show = True
            else:
                show = False

            # plot
            x = np.arange(len(error_vec)) + 1
            ax.plot(x, error_vec)
            if t_opt is not None:
                ax.plot(t_opt, error_vec[t_opt - 1], 'ro', markersize=10,)
            ax.plot(x, np.full(len(error_vec), threshold), 'k--')
            ax.set_xlabel('t')
            ax.set_ylabel('disparity(data_{t}, data_{t-1})')
            ax.set_xlim([1, len(error_vec)])
            plt.tight_layout()
            tasklogger.log_complete("optimal t plot")
            if show:
                plt.show(block=False)

        return data_imputed

    def knnDREMI(self, gene_x, gene_y,
                 k=10, n_bins=20, n_mesh=3, n_jobs=1,
                 plot=False, **kwargs):
        """Calculate kNN-DREMI on MAGIC output

        Calculates k-Nearest Neighbor conditional Density Resampled Estimate of
        Mutual Information as defined in Van Dijk et al, 2018. [1]_

        Note that kNN-DREMI, like Mutual Information and DREMI, is not
        symmetric. Here we are estimating I(Y|X).

        Parameters
        ----------
        gene_x : array-like, shape=[n_samples]
            Gene shown on the x axis (independent feature)
        gene_y : array-like, shape=[n_samples]
            Gene shown on the y axis (dependent feature)
        k : int, range=[0:n_samples), optional (default: 10)
            Number of neighbors
        n_bins : int, range=[0:inf), optional (default: 20)
            Number of bins for density resampling
        n_mesh : int, range=[0:inf), optional (default: 3)
            In each bin, density will be calculcated around (mesh ** 2) points
        n_jobs : int, optional (default: 1)
            Number of threads used for kNN calculation
        plot : bool, optional (default: False)
            If True, DREMI create plots of the data like those seen in
            Fig 5C/D of van Dijk et al. 2018. (doi:10.1016/j.cell.2018.05.061).
        **kwargs : additional arguments for `scprep.stats.plot_knnDREMI`

        Returns
        -------
        dremi : float
            kNN condtional Density resampled estimate of mutual information
        """
        data = self.transform(genes=[gene_x, gene_y])
        dremi = scprep.stats.knnDREMI(
            data[gene_x], data[gene_y],
            k=k, n_bins=n_bins, n_mesh=n_mesh, n_jobs=n_jobs,
            plot=plot, **kwargs)
        return dremi
