import logging

from keras import Input
from keras import Model
from keras.layers import add
from keras.layers import Dense
from keras.layers import Lambda
from keras.optimizers import SGD
from keras.regularizers import l2
from sklearn.utils import check_random_state

from csrank.constants import allowed_dense_kwargs
from csrank.layers import NormalizedDense
from csrank.learner import Learner

logger = logging.getLogger(__name__)


class RankNetCore(Learner):
    def __init__(
        self,
        n_hidden=2,
        n_units=8,
        loss_function="binary_crossentropy",
        batch_normalization=True,
        kernel_regularizer=l2,
        kernel_initializer="lecun_normal",
        activation="relu",
        optimizer=SGD,
        metrics=("binary_accuracy",),
        batch_size=256,
        random_state=None,
        **kwargs,
    ):
        self.batch_normalization = batch_normalization
        self.activation = activation
        self.metrics = metrics
        self.kernel_regularizer = kernel_regularizer
        self.kernel_initializer = kernel_initializer
        self.loss_function = loss_function
        self.optimizer = optimizer
        self.n_hidden = n_hidden
        self.n_units = n_units
        keys = list(kwargs.keys())
        for key in keys:
            if key not in allowed_dense_kwargs:
                del kwargs[key]
        self.kwargs = kwargs
        self.batch_size = batch_size
        self._scoring_model = None
        self.random_state = random_state

    def _construct_layers(self, **kwargs):
        logger.info("n_hidden {}, n_units {}".format(self.n_hidden, self.n_units))
        self.x1 = Input(shape=(self.n_object_features_fit_,))
        self.x2 = Input(shape=(self.n_object_features_fit_,))
        self.output_node = Dense(
            1, activation="sigmoid", kernel_regularizer=self.kernel_regularizer_
        )
        self.output_layer_score = Dense(1, activation="linear")
        if self.batch_normalization:
            self.hidden_layers = [
                NormalizedDense(self.n_units, name="hidden_{}".format(x), **kwargs)
                for x in range(self.n_hidden)
            ]
        else:
            self.hidden_layers = [
                Dense(self.n_units, name="hidden_{}".format(x), **kwargs)
                for x in range(self.n_hidden)
            ]
        assert len(self.hidden_layers) == self.n_hidden

    def construct_model(self):
        """
            Construct the RankNet which is used to approximate the :math:`U(x)` utility. For each pair of objects in
            :math:`x_i, x_j \\in Q` we construct two sub-networks with weight sharing in all hidden layer apart form the
            last layer for which weights are mirrored version of each other. The output of these networks are connected
            to a sigmoid unit that produces the output :math:`P_{ij}` which is the probability of preferring object
            :math:`x_i` over :math:`x_j`, to approximate the :math:`U(x)`.

            Returns
            -------
            model: keras :class:`Model`
                Neural network to learn the RankNet utility score
        """
        # weight sharing using same hidden layer for two objects
        enc_x1 = self.hidden_layers[0](self.x1)
        enc_x2 = self.hidden_layers[0](self.x2)
        neg_x2 = Lambda(lambda x: -x)(enc_x2)
        for hidden_layer in self.hidden_layers[1:]:
            enc_x1 = hidden_layer(enc_x1)
            neg_x2 = hidden_layer(neg_x2)
        merged_inputs = add([enc_x1, neg_x2])
        output = self.output_node(merged_inputs)
        model = Model(inputs=[self.x1, self.x2], outputs=output)
        model.compile(
            loss=self.loss_function,
            optimizer=self.optimizer_,
            metrics=list(self.metrics),
        )
        return model

    def _convert_instances_(self, X, Y):
        raise NotImplementedError

    def fit(
        self, X, Y, epochs=10, callbacks=None, validation_split=0.1, verbose=0, **kwd
    ):
        """
            Fit a preference learning RankNet model on a provided set of queries. The provided queries can be of
            a fixed size (numpy arrays). For learning this network the binary cross entropy loss function for a pair of
            objects :math:`x_i, x_j \\in Q` is defined as:

            .. math::

                C_{ij} =  -\\tilde{P_{ij}}\\log(P_{ij}) - (1 - \\tilde{P_{ij}})\\log(1 - P{ij}) \\enspace,

            where :math:`\\tilde{P_{ij}}` is ground truth probability of the preference of :math:`x_i` over :math:`x_j`.
            :math:`\\tilde{P_{ij}} = 1` if :math:`x_i \\succ x_j` else :math:`\\tilde{P_{ij}} = 0`.

            Parameters
            ----------
            X : numpy array (n_instances, n_objects, n_features)
                Feature vectors of the objects
            Y : numpy array (n_instances, n_objects)
                Preferences in form of Orderings or Choices for given n_objects
            epochs : int
                Number of epochs to run if training for a fixed query size
            callbacks : list
                List of callbacks to be called during optimization
            validation_split : float (range : [0,1])
                Percentage of instances to split off to validate on
            verbose : bool
                Print verbose information
            **kwd :
                Keyword arguments for the fit function
        """
        self.random_state_ = check_random_state(self.random_state)
        _n_instances, self.n_objects_fit_, self.n_object_features_fit_ = X.shape
        X1, X2, Y_single = self._convert_instances_(X, Y)

        logger.debug("Instances created {}".format(X1.shape[0]))
        logger.debug("Creating the model")

        self._initialize_optimizer()
        self._initialize_regularizer()
        self._construct_layers(
            kernel_regularizer=self.kernel_regularizer_,
            kernel_initializer=self.kernel_initializer,
            activation=self.activation,
            **self.kwargs,
        )

        # Model with input as two objects and output as probability of x1>x2
        self.model_ = self.construct_model()
        logger.debug("Finished Creating the model, now fitting started")

        self.model_.fit(
            [X1, X2],
            Y_single,
            batch_size=self.batch_size,
            epochs=epochs,
            callbacks=callbacks,
            validation_split=validation_split,
            verbose=verbose,
            **kwd,
        )

        logger.debug("Fitting Complete")

    @property
    def scoring_model(self):
        """
            Creates a scoring model for the trained ListNet, which predicts the utility scores for given set of objects.
            Returns
            -------
             model: keras :class:`Model`
                Neural network to learn the non-linear utility score
        """
        if self._scoring_model is None:
            logger.info("creating scoring model")
            inp = Input(shape=(self.n_object_features_fit_,))
            x = inp
            for hidden_layer in self.hidden_layers:
                x = hidden_layer(x)
            output_score = self.output_node(x)
            self._scoring_model = Model(inputs=[inp], outputs=output_score)
        return self._scoring_model

    def _predict_scores_fixed(self, X, **kwargs):
        n_instances, n_objects, n_features = X.shape
        logger.info("Test Set instances {} objects {} features {}".format(*X.shape))
        X1 = X.reshape(n_instances * n_objects, n_features)
        scores = self.scoring_model.predict(X1, **kwargs)
        scores = scores.reshape(n_instances, n_objects)
        logger.info("Done predicting scores")
        return scores
