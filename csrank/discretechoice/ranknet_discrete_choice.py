import logging

from keras.optimizers import SGD
from keras.regularizers import l2

from csrank.choicefunction.util import generate_complete_pairwise_dataset
from csrank.core.ranknet_core import RankNetCore
from .discrete_choice import DiscreteObjectChooser

logger = logging.getLogger(__name__)


class RankNetDiscreteChoiceFunction(DiscreteObjectChooser, RankNetCore):
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
        """
            Create an instance of the :class:`RankNetCore` architecture for learning a choice function.
            It breaks the preferences into pairwise comparisons and learns a latent utility model for the objects.
            This network learns a latent utility score for each object in the given query set
            :math:`Q = \\{x_1, \\ldots ,x_n\\}` using the equation :math:`U(x) = F(x, w)` where :math:`w` is the weight
            vector. It is estimated using *pairwise preferences* generated from the discrete choices.

            The discrete choice for the given query set :math:`Q` is defined as:

            .. math::

                ρ(Q)  = \\operatorname{argsort}_{x \\in Q}  \\; U(x)

            Parameters
            ----------
            n_hidden : int
                Number of hidden layers used in the scoring network
            n_units : int
                Number of hidden units in each layer of the scoring network
            loss_function : function or string
                Loss function to be used for the binary decision task of the pairwise comparisons
            batch_normalization : bool
                Whether to use batch normalization in each hidden layer
            kernel_regularizer : uninitialized keras regularizer
                Regularizer function applied to all the hidden weight matrices.
            kernel_initializer : function or string
                Initialization function for the weights of each hidden layer
            activation : function or string
                Type of activation function to use in each hidden layer
            optimizer: Class
                Uninitialized optimizer class following the keras optimizer interface.
            optimizer__{kwarg}
                Arguments to be passed to the optimizer on initialization, such as optimizer__lr.
            metrics : list
                List of metrics to evaluate during training (can be non-differentiable)
            batch_size : int
                Batch size to use during training
            random_state : int, RandomState instance or None
                Seed of the pseudo-random generator or a RandomState instance
            **kwargs
                Keyword arguments for the algorithms

            References
            ----------
                [1] Burges, C. et al. (2005, August). "Learning to rank using gradient descent.", In Proceedings of the 22nd international conference on Machine learning (pp. 89-96). ACM.

                [2] Burges, C. J. (2010). "From ranknet to lambdarank to lambdamart: An overview.", Learning, 11(23-581).
        """
        super().__init__(
            n_hidden=n_hidden,
            n_units=n_units,
            loss_function=loss_function,
            batch_normalization=batch_normalization,
            kernel_regularizer=kernel_regularizer,
            kernel_initializer=kernel_initializer,
            activation=activation,
            optimizer=optimizer,
            metrics=metrics,
            batch_size=batch_size,
            random_state=random_state,
            **kwargs,
        )
        logger.info("Initializing network")

    def _convert_instances_(self, X, Y):
        logger.debug("Creating the Dataset")
        x1, x2, garbage, garbage, y_single = generate_complete_pairwise_dataset(X, Y)
        del garbage
        logger.debug("Finished the Dataset instances {}".format(x1.shape[0]))
        return x1, x2, y_single
