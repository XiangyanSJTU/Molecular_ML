import numpy as np
import pandas as pd
from sklearn.metrics import mean_squared_error
from sklearn.metrics import explained_variance_score
import sklearn.gaussian_process as gp
from sklearn.cluster import SpectralClustering
import os
class ActiveLearner:
    ''' for active learning, basically do selection for users '''
    def __init__(self, train_X, train_Y, test_X, test_Y, initial_size, add_size, kernel_config, learning_mode, train_SMILES, search_size=50):
        ''' df must have the 'graph' column '''
        self.train_X = train_X.reset_index().drop(columns='index')
        self.train_Y = train_Y.reset_index().drop(columns='index')
        self.test_X = test_X
        self.test_Y = test_Y
        self.current_size = initial_size
        self.add_size = add_size
        self.search_size = search_size
        self.kernel_config = kernel_config
        self.learning_mode = learning_mode
        self.logger = open('active_learning.log', 'w')
        self.plotout = pd.DataFrame({'size': [], 'mse': [], 'r2': [], 'ex-var': [], 'alpha': []})
        self.train_SMILES = train_SMILES.reset_index().drop(columns='index')
        self.unique_smiles = train_SMILES.unique()
        self.train_smiles = np.random.choice(self.unique_smiles, initial_size, replace=False)

    def stop_sign(self, max_size):
        if self.current_size > max_size:
            return True
        elif self.current_size == len(self.train_X):
            return True
        else:
            return False

    def train(self, alpha=0.5):
        # continue needs to be added soon
        self.logger.write('Start Training, training size = %i:\n' % len(self.train_smiles))
        self.logger.write('training smiles: %s\n' % ' '.join(self.train_smiles))
        train_x = self.train_X[self.train_SMILES.SMILES.isin(self.train_smiles)]
        if not self.kernel_config.T:
            train_x = train_x['graph']
        train_y = self.train_Y[self.train_SMILES.SMILES.isin(self.train_smiles)]
        while alpha <= 10:
            try:
                self.model = gp.GaussianProcessRegressor(kernel=self.kernel_config.kernel, random_state=0,
                                                         normalize_y=True, alpha=alpha).fit(train_x, train_y)
            except ValueError as e:
                alpha *= 1.5
            else:
                break
        self.alpha = alpha
        self.logger.write('training complete, alpha=%3g\n' % alpha)

    def add_samples(self, add_size=None, search_size=None):
        if add_size is None:
            add_size = self.add_size
        untrain_x = self.train_X[~self.train_SMILES.SMILES.isin(self.train_smiles)]
        if not self.kernel_config.T:
            untrain_x = untrain_x['graph']
        untrain_y = self.train_Y[~self.train_SMILES.SMILES.isin(self.train_smiles)]
        untrain_smiles = self.train_SMILES[~self.train_SMILES.SMILES.isin(self.train_smiles)]
        if self.learning_mode == 'supervised':
            y_pred = self.model.predict(untrain_x)
            try:
                untrain_smiles['mse'] = abs(y_pred - np.array(untrain_y))
                group = untrain_smiles.groupby('SMILES')
                smiles_mse = pd.DataFrame({'SMILES': [], 'mse': []})
                for i, x in enumerate(group):
                    smiles_mse.loc[i] = x[0], x[1].mse.max()
            except:
                raise ValueError('Missing value for supervised training')
            index = smiles_mse['mse'].nlargest(add_size).index
            self.train_smiles = np.r_[self.train_smiles, smiles_mse[smiles_mse.index.isin(index)].SMILES]
        elif self.learning_mode == 'unsupervised':
            y_pred, y_std = self.model.predict(untrain_x, return_std=True)
            untrain_smiles['std'] = y_std
            group = untrain_smiles.groupby('SMILES')
            smiles_std = pd.DataFrame({'SMILES': [], 'std': [],'graph':[] })
            for i, x in enumerate(group):
                smiles_std.loc[i] = x[0], x[1]['std'].max(), self.train_X[self.train_SMILES.SMILES == x[0]]['graph'].tolist()[0]
            #index = smiles_std['std'].nlargest(add_size).index
            if search_size is None:
                    search_size = self.search_size
            if len(smiles_std) < search_size:
                search_size = len(smiles_std)
            search_idx = sorted(smiles_std['std'].nlargest(search_size).index)
            search_graphs = smiles_std[smiles_std.index.isin(search_idx)]['graph']
            add_idx = self._find_add_idx(search_graphs, add_size)
            self.train_smiles = np.r_[self.train_smiles, smiles_std[smiles_std.index.isin(add_idx)].SMILES]
        elif self.learning_mode == 'random':
            group = untrain_smiles.groupby('SMILES')
            smiles = pd.DataFrame({'SMILES': []})
            for i, x in enumerate(group):
                smiles.loc[i] = x[0]
            self.train_smiles = np.r_[self.train_smiles, np.random.choice(smiles.SMILES, self.add_size, replace=False)]
        else:
            raise ValueError("unrecognized method. Could only be one of ('supervised','unsupervised','random').")
        self.current_size = len(self.train_smiles)
        self.logger.write('samples added to training set, currently %d samples\n' % self.current_size)

    def _find_add_idx(self, X, add_sample_size):
        ''' find representative samples from a pool using clustering method
        :X: a list of graphs
        :add_sample_size: add sample size
        '''
        # train SpectralClustering on X
        if len(X) < add_sample_size:
            return [ i for i in range( len(X))]
        gram_matrix = self.kernel_config.kernel(X)
        result = SpectralClustering(n_clusters=add_sample_size, affinity='precomputed').fit_predict(gram_matrix) # cluster result
        # choose the one with least in cluster distance sum in each cluster
        for i in range(len(X)): # edit self distance to be zero
            gram_matrix[i][i] = np.inf
        total_distance = {i:{} for i in range(add_sample_size)} # (key: cluster_idx, val: dict of (key:sum of distance, val:idx))
        for i in range(len(X)): # get all in-class distance sum of each item
            cluster_class = result[i]
            total_distance[cluster_class][np.sum((np.array(result) == cluster_class) * 1/gram_matrix[i])] = i
        add_idx = [total_distance[i][min(total_distance[i].keys())] for i in range(add_sample_size)] # find min-in-cluster-distance associated idx
        return add_idx

    def evaluate(self):
        y_pred = self.model.predict(self.test_X)
        # R2
        r2 = self.model.score(self.test_X, self.test_Y)
        # MSE
        mse = mean_squared_error(y_pred, self.test_Y)
        # variance explained
        ex_var = explained_variance_score(y_pred, self.test_Y)
        self.logger.write("R-square:%.3f\tMSE:%.3g\texplained_variance:%.3f\n\n" % (r2, mse, ex_var))
        self.plotout.loc[self.current_size] = self.current_size, mse, r2, ex_var, self.alpha

    def get_training_plot(self):
        if not os.path.exists(os.path.join(os.getcwd(),'result' )):
                os.makedirs(os.path.join(os.getcwd(), 'result'))
        self.plotout.reset_index().drop(columns='index').\
            to_csv('result/%s-%s-%d-%d.log' % (self.kernel_config.property, self.learning_mode, self.search_size, self.add_size), sep=' ', index=False)
