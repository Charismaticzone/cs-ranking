import logging
from itertools import permutations

import numpy as np
import tensorflow as tf
from keras import optimizers, Input, Model, backend as K
from keras.layers import Dense, concatenate
from keras.optimizers import Adam
from keras.regularizers import l2
from sklearn.utils import check_random_state

from csrank.constants import allowed_dense_kwargs, THRESHOLD
from csrank.layers import NormalizedDense
from csrank.learner import Learner
from csrank.objectranking.object_ranker import ObjectRanker
from csrank.util import print_dictionary


class CmpNetCore(ObjectRanker, Learner):
    def __init__(self, n_object_features, hash_file, n_hidden=2, n_units=8,
                 loss_function='binary_crossentropy', batch_normalization=True,
                 kernel_regularizer=l2(l=1e-4), kernel_initializer='lecun_normal', activation='relu',
                 optimizer=Adam(), metrics=['binary_accuracy'],
                 batch_size=256, random_state=None, **kwargs):
        """
            Create an instance of the CmpNet architecture.

            CmpNet breaks the rankings into pairwise comparisons and learns a pairwise model for the each pair of object in the ranking.
            For prediction list of objects is converted in pair of objects and the pairwise predicate is evaluated using them.
            The outputs of the network for each pair of objects :math:`U(x_1,x_2), U(x_2,x_1)` are evaluated.
            :math:`U(x_1,x_2)` is a measure of how favorable it is for :math:`x_1` than :math:`x_2`.
            Ranking for the given set of objects :math:`Q = \{ x_1 , \ldots , x_n \}`  is evaluted as follows:

            .. math::

               ρ(Q) = \operatorname{argsort}_{i \in [n]}  \; \left\{ \\frac{1}{n-1} \sum_{j \in [n] \setminus \{i\}} U_1(x_i , x_j)\\right\}


            Parameters
            ----------
            n_object_features : int
                Number of features of the object space
            hash_file: str
                File path of the model where the weights are stored to get the predictions after clearing the memory
            n_hidden : int
                Number of hidden layers used in the scoring network
            n_units : int
                Number of hidden units in each layer of the scoring network
            loss_function : function or string
                Loss function to be used for the binary decision task of the
                pairwise comparisons
            batch_normalization : bool
                Whether to use batch normalization in each hidden layer
            kernel_regularizer : function
                Regularizer function applied to all the hidden weight matrices.
            activation : function or string
                Type of activation function to use in each hidden layer
            optimizer : function or string
                Optimizer to use during stochastic gradient descent
            metrics : list
                List of metrics to evaluate during training (can be
                non-differentiable)
            batch_size : int
                Batch size to use during training
            random_state : int, RandomState instance or None
                Seed of the pseudorandom generator or a RandomState instance
            **kwargs
                Keyword arguments for the algorithms

            References
            ----------
            .. [1] Leonardo Rigutini, Tiziano Papini, Marco Maggini, and Franco Scarselli. 2011.
               SortNet: Learning to Rank by a Neural Preference Function.
               IEEE Trans. Neural Networks 22, 9 (2011), 1368–1380. https://doi.org/10.1109/TNN.2011.2160875
        """
        self.logger = logging.getLogger("CmpNet")
        self.n_object_features = n_object_features
        self.batch_normalization = batch_normalization
        self.activation = activation
        self.hash_file = hash_file

        self.batch_size = batch_size

        self.metrics = metrics
        self.kernel_regularizer = kernel_regularizer
        self.kernel_initializer = kernel_initializer
        self.loss_function = loss_function

        self.optimizer = optimizers.get(optimizer)
        self._optimizer_config = self.optimizer.get_config()

        self.n_hidden = n_hidden
        self.n_units = n_units
        keys = list(kwargs.keys())
        for key in keys:
            if key not in allowed_dense_kwargs:
                del kwargs[key]
        self.kwargs = kwargs
        self.threshold_instances = THRESHOLD
        self.random_state = check_random_state(random_state)
        self.model = None
        self._construct_layers(kernel_regularizer=self.kernel_regularizer, kernel_initializer=self.kernel_initializer,
                               activation=self.activation, **self.kwargs)

    def _construct_layers(self, **kwargs):

        self.output_node = Dense(1, activation='sigmoid', kernel_regularizer=self.kernel_regularizer)
        self.x1 = Input(shape=(self.n_object_features,))
        self.x2 = Input(shape=(self.n_object_features,))
        if self.batch_normalization:
            self.hidden_layers = [NormalizedDense(self.n_units, name="hidden_{}".format(x), **kwargs) for x in
                                  range(self.n_hidden)]
        else:
            self.hidden_layers = [Dense(self.n_units, name="hidden_{}".format(x), **kwargs) for x in
                                  range(self.n_hidden)]
        assert len(self.hidden_layers) == self.n_hidden

    def fit(self, X, Y, epochs=10, callbacks=None,
            validation_split=0.1, verbose=0, **kwd):

        x1, x2, y_double = self.convert_instances(X, Y)

        merged_output = self.construct_model()

        self.model = Model(inputs=[self.x1, self.x2], outputs=merged_output)
        self.model.compile(loss=self.loss_function, optimizer=self.optimizer, metrics=self.metrics)

        self.logger.debug('Finished Creating the model, now fitting started')

        self.model.fit([x1, x2], y_double, batch_size=self.batch_size, epochs=epochs,
                       callbacks=callbacks, validation_split=validation_split,
                       verbose=verbose, **kwd)
        self.logger.debug('Fitting Complete')

    def convert_instances(self, X, Y):
        raise NotImplemented

    def construct_model(self):
        x1x2 = concatenate([self.x1, self.x2])
        x2x1 = concatenate([self.x2, self.x1])
        self.logger.debug('Creating the model')
        for hidden in self.hidden_layers:
            x1x2 = hidden(x1x2)
            x2x1 = hidden(x2x1)
        merged_left = concatenate([x1x2, x2x1])
        merged_right = concatenate([x2x1, x1x2])
        N_g = self.output_node(merged_left)
        N_l = self.output_node(merged_right)
        merged_output = concatenate([N_g, N_l])
        return merged_output

    def predict_pair(self, a, b, **kwargs):
        return self.model.predict([a, b], **kwargs)

    def _predict_scores_fixed(self, X, **kwargs):
        n_instances, n_objects, n_features = X.shape
        self.logger.info("For Test instances {} objects {} features {}".format(n_instances, n_objects, n_features))
        n2 = n_objects * (n_objects - 1)
        pairs = np.empty((n2, 2, n_features))
        scores = np.empty((n_instances, n_objects))
        for n in range(n_instances):
            for k, (i, j) in enumerate(permutations(range(n_objects), 2)):
                pairs[k] = (X[n, i], X[n, j])
            result = self.predict_pair(pairs[:, 0], pairs[:, 1], **kwargs)[:, 0]
            scores[n] = result.reshape(n_objects, n_objects - 1).mean(axis=1)
            del result
        del pairs
        self.logger.info("Done predicting scores")

        return scores

    def clear_memory(self, **kwargs):
        self.model.save_weights(self.hash_file)
        K.clear_session()
        sess = tf.Session()
        K.set_session(sess)

        self.optimizer = self.optimizer.from_config(self._optimizer_config)
        self._construct_layers(kernel_regularizer=self.kernel_regularizer, kernel_initializer=self.kernel_initializer,
                               activation=self.activation, **self.kwargs)
        output = self.construct_model()
        self.model = Model(inputs=[self.x1, self.x2], outputs=output)
        self.model.load_weights(self.hash_file)

    def set_tunable_parameters(self, n_hidden=32,
                               n_units=2,
                               reg_strength=1e-4,
                               learning_rate=1e-3,
                               batch_size=128, **point):
        self.n_hidden = n_hidden
        self.n_units = n_units
        self.kernel_regularizer = l2(reg_strength)
        self.batch_size = batch_size
        self.optimizer = self.optimizer.from_config(self._optimizer_config)
        K.set_value(self.optimizer.lr, learning_rate)
        self._construct_layers(kernel_regularizer=self.kernel_regularizer, kernel_initializer=self.kernel_initializer,
                               activation=self.activation, **self.kwargs)
        if len(point) > 0:
            self.logger.warning('This ranking algorithm does not support'
                                ' tunable parameters called: {}'.format(print_dictionary(point)))