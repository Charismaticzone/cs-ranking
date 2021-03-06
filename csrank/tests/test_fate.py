from abc import ABCMeta

from keras import Input
from keras import Model
from keras.regularizers import l2
import numpy as np

from csrank import FATENetworkCore
from csrank import FATEObjectRanker
from csrank.tests.test_ranking import optimizer_common_args


def test_construction_core():
    n_objects = 3
    n_features = 2

    # Create mock class:

    class MockClass(FATENetworkCore, metaclass=ABCMeta):
        def predict_scores(self, X, **kwargs):
            pass

        def _predict_scores_fixed(self, X, **kwargs):
            pass

        def predict(self, *args, **kwargs):
            pass

        def fit(self, *args, **kwargs):
            return self

    grc = MockClass()
    grc._initialize_optimizer()
    grc._initialize_regularizer()
    grc._construct_layers()
    input_layer = Input(shape=(n_objects, n_features))
    scores = grc.join_input_layers(input_layer, None, n_layers=0, n_objects=n_objects)

    model = Model(inputs=input_layer, outputs=scores)
    model.compile(loss="mse", optimizer=grc.optimizer_)
    X = np.random.randn(100, n_objects, n_features)
    y = X.sum(axis=2)
    model.fit(x=X, y=y, verbose=0)


def test_fate_object_ranker_fixed_generator():
    def trivial_ranking_problem_generator():
        while True:
            rand = np.random.RandomState(123)
            x = rand.randn(10, 5, 1)
            y_true = x.argsort(axis=1).argsort(axis=1).squeeze(axis=-1)
            yield x, y_true

    fate = FATEObjectRanker(
        n_hidden_joint_layers=1,
        n_hidden_set_layers=1,
        n_hidden_joint_units=5,
        n_hidden_set_units=5,
        kernel_regularizer=l2,
        kernel_regularizer__l=1e-4,
        **optimizer_common_args,
    )
    fate.fit_generator(
        generator=trivial_ranking_problem_generator(),
        epochs=1,
        validation_split=0,
        verbose=False,
        steps_per_epoch=10,
    )
