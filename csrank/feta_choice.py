from csrank import FETANetwork

import numpy as np

from keras.losses import binary_crossentropy
from keras.regularizers import l2
from sklearn.model_selection import train_test_split
from sklearn.metrics import f1_score


class FETAChoiceFunction(FETANetwork):
    def __init__(self, n_objects, n_features, n_hidden=2, n_units=8,
                 add_zeroth_order_model=False, max_number_of_objects=5,
                 num_subsample=5, loss_function=binary_crossentropy,
                 batch_normalization=False, kernel_regularizer=l2(l=1e-4),
                 non_linearities='selu', optimizer="adam", metrics=None,
                 use_early_stopping=False, es_patience=300, batch_size=256,
                 random_state=None, **kwargs):
        super().__init__(n_objects, n_features, n_hidden, n_units,
                         add_zeroth_order_model, max_number_of_objects,
                         num_subsample, loss_function, batch_normalization,
                         kernel_regularizer, non_linearities, optimizer,
                         metrics, use_early_stopping, es_patience, batch_size,
                         random_state, **kwargs)
        self.threshold = 0.5

    def _tune_threshold(self, X_val, Y_val, thin_thresholds=1):
        scores = self.predict_scores(X_val)
        probs = np.unique(scores)[::thin_thresholds]
        threshold = 0.0
        best = f1_score(Y_val, scores > threshold, average='samples')
        for i, p in enumerate(probs):
            pred = scores > p
            f1 = f1_score(Y_val, pred, average='samples')
            if f1 > best:
                threshold = p
                best = f1
        self.logger.info('Tuned threshold, obtained {:.2f} which achieved'
                         ' a micro F1-measure of {:.2f}'.format(
            threshold, best))
        return threshold

    def fit(self, X, Y, epochs=10, callbacks=None, validation_split=0.1,
            tune_size=0.1, thin_thresholds=1, verbose=0, **kwd):
        if tune_size > 0:
            X_train, X_val, Y_train, Y_val = train_test_split(
                X, Y, test_size=tune_size)
            try:
                super().fit(X_train, Y_train, epochs, callbacks,
                            validation_split, verbose, **kwd)
            finally:
                self.logger.info('Fitting utility function finished.'
                                 ' Start tuning threshold.')
                self.threshold = self._tune_threshold(
                    X_val, Y_val,
                    thin_thresholds=thin_thresholds)
        else:
            super().fit(X, Y, epochs, callbacks, validation_split, verbose,
                        **kwd)
            self.threshold = 0.5

    def predict_scores(self, X, **kwargs):
        return super().predict_scores(X, **kwargs)

    def predict(self, X, **kwargs):
        scores = self.predict_scores(X, **kwargs)
        return scores > self.threshold

    def __call__(self, X, **kwargs):
        return self.predict(X, **kwargs)