from itertools import combinations
from itertools import permutations
import logging

from keras import backend as K
from keras import Input
from keras import Model
from keras.layers import Activation
from keras.layers import concatenate
from keras.layers import Dense
from keras.layers import Lambda
from keras.optimizers import SGD
from keras.regularizers import l2
import numpy as np

from csrank.core.feta_network import FETANetwork
from csrank.layers import NormalizedDense
from csrank.numpy_util import sigmoid
from .discrete_choice import DiscreteObjectChooser

logger = logging.getLogger(__name__)


class FETADiscreteChoiceFunction(DiscreteObjectChooser, FETANetwork):
    def __init__(
        self,
        n_hidden=2,
        n_units=8,
        add_zeroth_order_model=False,
        max_number_of_objects=10,
        num_subsample=5,
        loss_function="categorical_hinge",
        batch_normalization=False,
        kernel_regularizer=l2,
        kernel_initializer="lecun_normal",
        activation="selu",
        optimizer=SGD,
        metrics=("categorical_accuracy",),
        batch_size=256,
        random_state=None,
        **kwargs,
    ):
        """
            Create a FETA-network architecture for learning the discrete choice functions.
            The first-evaluate-then-aggregate approach approximates the context-dependent utility function using the
            first-order utility function :math:`U_1 \\colon \\mathcal{X} \\times \\mathcal{X} \\rightarrow [0,1]`
            and zeroth-order utility function  :math:`U_0 \\colon \\mathcal{X} \\rightarrow [0,1]`.
            The scores each object :math:`x` using a context-dependent utility function :math:`U (x, C_i)`:

            .. math::
                 U(x_i, C_i) = U_0(x_i) + \\frac{1}{n-1} \\sum_{x_j \\in Q \\setminus \\{x_i\\}} U_1(x_i , x_j) \\, .

            Training and prediction complexity is quadratic in the number of objects.
            The discrete choice for the given query set :math:`Q` is defined as:

            .. math::

                dc(Q) := \\operatorname{argmax}_{x_i \\in Q}  \\;  U (x_i, C_i)

            Parameters
            ----------
            n_hidden : int
                Number of hidden layers
            n_units : int
                Number of hidden units in each layer
            add_zeroth_order_model : bool
                True if the model should include a latent utility function
            max_number_of_objects : int
                The maximum number of objects to train from
            num_subsample : int
                Number of objects to subsample to
            loss_function : function
                Differentiable loss function for the score vector
            batch_normalization : bool
                Whether to use batch normalization in the hidden layers
            kernel_regularizer : uninitialized keras regularizer
                Regularizer to use in the hidden units
            kernel_initializer : function or string
                Initialization function for the weights of each hidden layer
            activation : string or function
                Activation function to use in the hidden units
            optimizer: Class
                Uninitialized optimizer class following the keras optimizer interface.
            optimizer__{kwarg}
                Arguments to be passed to the optimizer on initialization, such as optimizer__lr.
            metrics : list
                List of evaluation metrics (can be non-differentiable)
            batch_size : int
                Batch size to use for training
            random_state : int or object
                Numpy random state
            hidden_dense_layer__{kwarg}
                Arguments to be passed to the Dense layers (or NormalizedDense
                if batch_normalization is enabled). See the keras documentation
                for those classes for available options.
        """
        self._store_kwargs(
            kwargs, {"optimizer__", "kernel_regularizer__", "hidden_dense_layer__"}
        )
        super().__init__(
            n_hidden=n_hidden,
            n_units=n_units,
            add_zeroth_order_model=add_zeroth_order_model,
            max_number_of_objects=max_number_of_objects,
            num_subsample=num_subsample,
            loss_function=loss_function,
            batch_normalization=batch_normalization,
            kernel_regularizer=kernel_regularizer,
            kernel_initializer=kernel_initializer,
            activation=activation,
            optimizer=optimizer,
            metrics=metrics,
            batch_size=batch_size,
            random_state=random_state,
        )

    def _construct_layers(self):
        self.input_layer = Input(
            shape=(self.n_objects_fit_, self.n_object_features_fit_)
        )
        # Todo: Variable sized input
        # X = Input(shape=(None, n_features))
        hidden_dense_kwargs = {
            "kernel_regularizer": self.kernel_regularizer_,
            "kernel_initializer": self.kernel_initializer,
            "activation": self.activation,
        }
        hidden_dense_kwargs.update(self._get_prefix_attributes("hidden_dense_layer__"))
        if self.batch_normalization:
            if self.add_zeroth_order_model:
                self.hidden_layers_zeroth = [
                    NormalizedDense(
                        self.n_units,
                        name="hidden_zeroth_{}".format(x),
                        **hidden_dense_kwargs,
                    )
                    for x in range(self.n_hidden)
                ]
            self.hidden_layers = [
                NormalizedDense(
                    self.n_units, name="hidden_{}".format(x), **hidden_dense_kwargs
                )
                for x in range(self.n_hidden)
            ]
        else:
            if self.add_zeroth_order_model:
                self.hidden_layers_zeroth = [
                    Dense(
                        self.n_units,
                        name="hidden_zeroth_{}".format(x),
                        **hidden_dense_kwargs,
                    )
                    for x in range(self.n_hidden)
                ]
            self.hidden_layers = [
                Dense(self.n_units, name="hidden_{}".format(x), **hidden_dense_kwargs)
                for x in range(self.n_hidden)
            ]
        assert len(self.hidden_layers) == self.n_hidden
        self.output_node = Dense(
            1,
            activation="linear",
            kernel_regularizer=self.kernel_regularizer_,
            name="score",
        )
        if self.add_zeroth_order_model:
            self.output_node_zeroth = Dense(
                1,
                activation="linear",
                kernel_regularizer=self.kernel_regularizer_,
                name="zero_score",
            )
            self.weighted_sum = Dense(
                1,
                activation="sigmoid",
                kernel_regularizer=self.kernel_regularizer_,
                name="weighted_sum",
            )

    def construct_model(self):
        """
            Construct the :math:`1`-st order and :math:`0`-th order models, which are used to approximate the
            :math:`U_1(x, C(x))` and the :math:`U_0(x)` utilities respectively. For each pair of objects in
            :math:`x_i, x_j \\in Q` :math:`U_1(x, C(x))` we construct :class:`CmpNetCore` with weight sharing to
            approximate a pairwise-matrix. A pairwise matrix with index (i,j) corresponds to the :math:`U_1(x_i,x_j)`
            is a measure of how favorable it is to choose :math:`x_i` over :math:`x_j`. Using this matrix we calculate
            the borda score for each object to calculate :math:`U_1(x, C(x))`. For `0`-th order model we construct
            :math:`\\lvert Q \\lvert` sequential networks whose weights are shared to evaluate the :math:`U_0(x)` for
            each object in the query set :math:`Q`. The output mode is using sigmoid activation.

            Returns
            -------
            model: keras :class:`Model`
                Neural network to learn the FETA utility score
        """

        def create_input_lambda(i):
            return Lambda(lambda x: x[:, i])

        if self.add_zeroth_order_model:
            logger.debug("Create 0th order model")
            zeroth_order_outputs = []
            inputs = []
            for i in range(self.n_objects_fit_):
                x = create_input_lambda(i)(self.input_layer)
                inputs.append(x)
                for hidden in self.hidden_layers_zeroth:
                    x = hidden(x)
                zeroth_order_outputs.append(self.output_node_zeroth(x))
            zeroth_order_scores = concatenate(zeroth_order_outputs)
            logger.debug("0th order model finished")
        logger.debug("Create 1st order model")
        outputs = [list() for _ in range(self.n_objects_fit_)]
        for i, j in combinations(range(self.n_objects_fit_), 2):
            if self.add_zeroth_order_model:
                x1 = inputs[i]
                x2 = inputs[j]
            else:
                x1 = create_input_lambda(i)(self.input_layer)
                x2 = create_input_lambda(j)(self.input_layer)
            x1x2 = concatenate([x1, x2])
            x2x1 = concatenate([x2, x1])

            for hidden in self.hidden_layers:
                x1x2 = hidden(x1x2)
                x2x1 = hidden(x2x1)

            merged_left = concatenate([x1x2, x2x1])
            merged_right = concatenate([x2x1, x1x2])

            N_g = self.output_node(merged_left)
            N_l = self.output_node(merged_right)

            outputs[i].append(N_g)
            outputs[j].append(N_l)
        # convert rows of pairwise matrix to keras layers:
        outputs = [concatenate(x) for x in outputs]

        # compute utility scores:
        scores = [
            Lambda(lambda s: K.mean(s, axis=1, keepdims=True))(x) for x in outputs
        ]
        scores = concatenate(scores)
        logger.debug("1st order model finished")
        if self.add_zeroth_order_model:

            def get_score_object(i):
                return Lambda(lambda x: x[:, i, None])

            concat_scores = [
                concatenate(
                    [
                        get_score_object(i)(scores),
                        get_score_object(i)(zeroth_order_scores),
                    ]
                )
                for i in range(self.n_objects_fit_)
            ]
            scores = []
            for i in range(self.n_objects_fit_):
                scores.append(self.weighted_sum(concat_scores[i]))
            scores = concatenate(scores)

        # if self.add_zeroth_order_model:
        #     scores = add([scores, zeroth_order_scores])
        # if self.add_zeroth_order_model:
        #     def expand_dims():
        #         return Lambda(lambda x: x[..., None])
        #
        #     def squeeze_dims():
        #         return Lambda(lambda x: x[:, :, 0])
        #
        #     scores = expand_dims()(scores)
        #     zeroth_order_scores = expand_dims()(zeroth_order_scores)
        #     concat_scores = concatenate([scores, zeroth_order_scores], axis=-1)
        #     weighted_sum = Conv1D(name='weighted_sum', filters=1, kernel_size=(1), strides=1, activation='linear',
        #                          kernel_initializer=self.kernel_initializer, input_shape=(self.n_objects_fit_, 2),
        #                          kernel_regularizer=self.kernel_regularizer, use_bias=False)
        #     scores = weighted_sum(concat_scores)
        #     scores = squeeze_dims()(scores)
        if not self.add_zeroth_order_model:
            scores = Activation("sigmoid")(scores)
        model = Model(inputs=self.input_layer, outputs=scores)
        logger.debug("Compiling complete model...")
        model.compile(
            loss=self.loss_function,
            optimizer=self.optimizer_,
            metrics=list(self.metrics),
        )
        return model

    def _create_weighted_model(self, n_objects):
        def get_score_object(i):
            return Lambda(lambda x: x[:, i, None])

        s1 = Input(shape=(n_objects,))
        s2 = Input(shape=(n_objects,))
        concat_scores = [
            concatenate([get_score_object(i)(s1), get_score_object(i)(s2)])
            for i in range(n_objects)
        ]
        scores = []
        for i in range(n_objects):
            scores.append(self.weighted_sum(concat_scores[i]))
        scores = concatenate(scores)
        model = Model(inputs=[s1, s2], outputs=scores)
        return model

    def _predict_scores_using_pairs(self, X, **kwd):
        n_instances, n_objects, n_features = X.shape
        n2 = n_objects * (n_objects - 1)
        pairs = np.empty((n2, 2, n_features))
        scores = np.zeros((n_instances, n_objects))
        for n in range(n_instances):
            for k, (i, j) in enumerate(permutations(range(n_objects), 2)):
                pairs[k] = (X[n, i], X[n, j])
            result = self._predict_pair(
                pairs[:, 0], pairs[:, 1], only_pairwise=True, **kwd
            )[:, 0]
            scores[n] += result.reshape(n_objects, n_objects - 1).mean(axis=1)
            del result
        del pairs
        if self.add_zeroth_order_model:
            scores_zero = self.zero_order_model.predict(X.reshape(-1, n_features))
            scores_zero = scores_zero.reshape(n_instances, n_objects)
            model = self._create_weighted_model(n_objects)
            scores = model.predict([scores, scores_zero], **kwd)
        else:
            scores = sigmoid(scores)
        return scores

    def _create_zeroth_order_model(self):
        inp = Input(shape=(self.n_object_features_fit_,))

        x = inp
        for hidden in self.hidden_layers_zeroth:
            x = hidden(x)
        zeroth_output = self.output_node_zeroth(x)

        return Model(inputs=[inp], outputs=Activation("sigmoid")(zeroth_output))
