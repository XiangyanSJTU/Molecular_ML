"""
Gaussian processes regression using Nystrom approximation.

The hyperparameter training process is designed to be self-consistent process. The K_core selection of Nystrom is
dependent on the kernel, and the kernel hyperparameter optimization is dependent on K_core.

The NystromGaussianProcessRegressor.fit_robust() need to be call several time to ensure convergence.

Drawbacks:
************************************************************************************************************************
The self-consistent process is not always converged. So how many loops are used is quite tricky.

For critical density prediction, it is not converged.
************************************************************************************************************************

Examples:
************************************************************************************************************************
N = 3  # N=1 for critical density.
for i in range(N):
    model = NystromGaussianProcessRegressor(kernel=kernel, random_state=0,
                                            kernel_cutoff=0.95, normalize_y=True,
                                            alpha=alpha).fit_robust(X, y)
    kernel = model.kernel_
************************************************************************************************************************
"""
from sklearn.gaussian_process._gpr import *
from sklearn.cluster import SpectralClustering
from numpy.linalg import eigh
import pandas as pd


def get_subset_by_clustering(X, kernel, ncluster):
    ''' find representative samples from a pool using clustering method
    :X: a list of graphs
    :add_sample_size: add sample size
    :return: list of idx
    '''
    # train SpectralClustering on X
    if len(X) < ncluster:
        return X
    gram_matrix = kernel(X)
    result = SpectralClustering(n_clusters=ncluster, affinity='precomputed').fit_predict(gram_matrix)  # cluster result
    total_distance = {i: {} for i in range(ncluster)}  # (key: cluster_idx, val: dict of (key:sum of distance, val:idx))
    for i in range(len(X)):  # get all in-class distance sum of each item
        cluster_class = result[i]
        total_distance[cluster_class][np.sum((np.array(result) == cluster_class) * 1 / gram_matrix[i])] = i
    add_idx = [total_distance[i][min(total_distance[i].keys())] for i in
               range(ncluster)]  # find min-in-cluster-distance associated idx
    return np.array(add_idx)


def Nystrom_solve(K_core, K_cross):
    Wcc, Ucc = np.linalg.eigh(K_core)
    mask = Wcc > 0  # !!!
    Wcc = Wcc[mask]  # !!!
    Ucc = Ucc[:, mask]  # !!!
    Kccinv = (Ucc / Wcc).dot(Ucc.T)
    Uxx, Sxx, Vxx = np.linalg.svd(K_cross.T.dot((Ucc / Wcc ** 0.5).dot(Ucc.T)), full_matrices=False)
    mask = Sxx > 1e-10  # !!!
    Uxx = Uxx[:, mask]  # !!!
    Sxx = Sxx[mask]  # !!!
    Kxx_ihalf = Uxx / Sxx
    return Kccinv, Kxx_ihalf


class RobustFitGaussianProcessRegressor(GaussianProcessRegressor):
    def fit_robust(self, X, y):
        while self.alpha < 100:
            try:
                print('Try to fit the data with alpha = %f' % self.alpha)
                self.fit(X, y)
                print('Success fit the data with alpha = %f\n' % self.alpha)
            except ValueError as e:
                print('error info: ', e)
                self.alpha *= 1.1
            else:
                break
        if self.alpha > 100:
            print('Attempted alpha larger than 100. The training is terminated for unstable numerical issues may occur.')
            return None
        else:
            return self


class NystromGaussianProcessRegressor(RobustFitGaussianProcessRegressor):
    def __init__(self, off_diagonal_cutoff=0.9, core_max=500, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.off_diagonal_cutoff = off_diagonal_cutoff
        self.core_max = core_max

    def __y_normalise(self, y):
        # Normalize target value
        if self.normalize_y:
            self._y_train_mean_ = np.mean(y, axis=0)
            # demean y
            y = y - self._y_train_mean
        else:
            self._y_train_mean_ = np.zeros(1)
        return y

    def fit_robust(self, X, y):
        print('Start a new fit process')
        if hasattr(self, 'kernel_'):
            X_, y_ = self.get_core_X(X, self.kernel_, off_diagonal_cutoff=self.off_diagonal_cutoff, y=y,
                                     core_max=self.core_max)
        else:
            X_, y_ = self.get_core_X(X, self.kernel, off_diagonal_cutoff=self.off_diagonal_cutoff, y=y,
                                     core_max=self.core_max)
        if super().fit_robust(X_, y_) is None:
            return None
        y = self.__y_normalise(y)
        self.X_train = np.copy(X) if self.copy_X_train else X
        self.y_train = np.copy(y) if self.copy_X_train else y
        return self

    def predict(self, X, return_std=False, return_cov=False):
        if return_std and return_cov:
            raise RuntimeError(
                "Not returning standard deviation of predictions when "
                "returning full covariance.")

        if self.kernel is None or self.kernel.requires_vector_input:
            X = check_array(X, ensure_2d=True, dtype="numeric")
        else:
            X = check_array(X, ensure_2d=False, dtype=None)

        if not hasattr(self, "X_train_"):  # Unfitted;predict based on GP prior
            if self.kernel is None:
                kernel = (C(1.0, constant_value_bounds="fixed") *
                          RBF(1.0, length_scale_bounds="fixed"))
            else:
                kernel = self.kernel
            y_mean = np.zeros(X.shape[0])
            if return_cov:
                y_cov = kernel(X)
                return y_mean, y_cov
            elif return_std:
                y_var = kernel.diag(X)
                return y_mean, np.sqrt(y_var)
            else:
                return y_mean
        else:  # Predict based on GP posterior
            K_core, K_cross = self.get_Nystrom_K(self.X_train, self.kernel_)
            Kccinv, Kxx_ihalf = Nystrom_solve(K_core, K_cross)
            Kyc = self.kernel_(X, self.core_X)
            left = Kyc.dot(Kccinv).dot(K_cross.dot(Kxx_ihalf))  # y*c
            right = Kxx_ihalf.T.dot(self.y_train)  # c*o
            y_mean = left.dot(right)
            y_mean = self._y_train_mean_ + y_mean  # undo normal.
            print('')
            if return_cov:
                y_cov = self.kernel_(X) - left.dot(left.T)  # Line 6
                return y_mean, y_cov
            elif return_std:
                # Compute variance of predictive distribution
                y_var = self.kernel_.diag(X)
                y_var -= np.einsum("ij,ij->i", left, left)
                # Check if any of the variances is negative because of
                # numerical issues. If yes: set the variance to 0.
                y_var_negative = y_var < 0
                if np.any(y_var_negative):
                    print('%i predicted variances smaller than 0' % len(y_var[y_var_negative]))
                    print('most negative value: %f' % min(y_var[y_var_negative]))
                    warnings.warn("Predicted variances smaller than 0. Setting those variances to 0.")
                    y_var[y_var_negative] = 0.0
                return y_mean, np.sqrt(y_var)
            else:
                return y_mean

    def core_predict(self, X, return_std=False, return_cov=False):
        # Precompute quantities required for predictions which are independent
        # of actual query points
        K = self.kernel_(self.X_train_)
        K[np.diag_indices_from(K)] += self.alpha
        try:
            self.L_ = cholesky(K, lower=True)  # Line 2
            # self.L_ changed, self._K_inv needs to be recomputed
            self._K_inv = None
        except np.linalg.LinAlgError as exc:
            exc.args = ("The kernel, %s, is not returning a "
                        "positive definite matrix. Try gradually "
                        "increasing the 'alpha' parameter of your "
                        "GaussianProcessRegressor estimator."
                        % self.kernel_,) + exc.args
            raise
        self.alpha_ = cho_solve((self.L_, True), self.y_train_)  # Line 3
        return super().predict(X, return_std, return_cov)

    def get_Nystrom_K(self, X, kernel, eval_gradient=False):
        np.random.seed(1)

        core_X = self.get_core_X(X, kernel, off_diagonal_cutoff=self.off_diagonal_cutoff, core_max=self.core_max)
        self.core_X = core_X
        # print(rand_idx[:n_components], rand_idx[n_components:])
        if eval_gradient:
            K_core, K_core_gradient = kernel(core_X, eval_gradient=True)
            K_cross, K_cross_gradient = kernel(core_X, X, eval_gradient=True)
            return K_core, K_core_gradient, K_cross, K_cross_gradient
        else:
            K_core = kernel(core_X)
            K_cross = kernel(core_X, X)
            return K_core, K_cross

    @staticmethod
    def get_core_X(X, kernel, off_diagonal_cutoff=0.9, y=None, core_max=500, method='suggest'):
        if X.__class__ == pd.DataFrame:
            X = X.to_numpy()
        N = X.shape[0]
        randN = np.array(list(range(N)))
        np.random.shuffle(randN)

        def get_C_idx(K, skip=0):
            K_diag = np.einsum("ii->i", K)
            C_idx_ = [0] if skip == 0 else list(range(skip))
            for m in range(K.shape[0]):
                # sys.stdout.write('\r %i / %i' % (i, N))
                if m >= skip and (K[m][C_idx_] / np.sqrt(K_diag[m] * K_diag[C_idx_])).max() < off_diagonal_cutoff:
                    C_idx_.append(m)
                if len(C_idx_) > core_max:
                    break
            return C_idx_[skip:]
        if method == 'suggest':
            """
            O(m2) complexity. Suggested.
            Best method now.
            This is a trade-off between full and memory_save. Fast and do not need much memory.
            """
            import math
            C_idx = np.array([], dtype=int)
            n = 200
            for i in range(math.ceil(N / n)):
                idx1 = np.r_[C_idx, randN[i*n:(i+1)*n]]
                idx2 = get_C_idx(kernel(X[idx1]), skip=len(C_idx))
                C_idx = np.r_[C_idx, idx1[idx2]]
                if len(C_idx) > core_max:
                    C_idx = C_idx[:core_max]
                    break
            print('%i / %i data are chosen as core in Nystrom approximation' % (len(C_idx), N))
        elif method == 'full':
            """
            O(n2) complexity. Suggest when X is not too large. 
            need to calculate the whole kernel matrix.
            Fastest in small sample cases. 
            """
            idx1 = randN
            idx2 = get_C_idx(kernel(X[idx1]))
            C_idx = idx1[idx2]
            print('%i / %i data are chosen as core in Nystrom approximation' % (len(C_idx), N))
        elif method == 'memory_save':
            """
            O(m2) complexity. Suggest when X is large.
            This is too slow due to call kernel function too many times. But few memory cost.
            """
            import sys
            C = X[:1]
            C_diag = kernel.diag(C)
            C_idx = []
            for i in randN:
                sys.stdout.write('\r %i / %i' % (i, N))
                diag = kernel.diag(X[i:i + 1])
                if (kernel(X[i:i + 1], C) / np.sqrt(diag * C_diag)).max() < off_diagonal_cutoff:
                    C = np.r_[C, X[i:i + 1]]
                    C_diag = np.r_[C_diag, diag]
                    C_idx.append(i)
                if len(C_idx) > core_max:
                    break
            print('\n%i / %i data are chosen as core in Nystrom approximation' % (len(C_idx), N))
        elif method == 'clustering':
            """
            Not suggest. 
            The clustering method is slow. No performance comparison has done. 
            """
            _C_idx = get_subset_by_clustering(X, kernel, ncluster=500)
            N = len(_C_idx)
            print('%i / %i data are chosen by clustering as core in Nystrom approximation' % (len(_C_idx), X.shape[0]))
            C_idx = get_C_idx(kernel(X[_C_idx]))
            print('%i / %i data are furthur selected to avoid numerical explosion' % (len(C_idx), N))
        elif method == 'random':
            C_idx = randN[:core_max]
        else:
            raise Exception('unknown method')
        if y is not None:
            return X[C_idx], y[C_idx]
        else:
            return X[C_idx]