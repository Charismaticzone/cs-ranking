from abc import ABCMeta
from abc import abstractmethod
import inspect
import logging

from keras.layers import Dense
from sklearn.base import BaseEstimator

logger = logging.getLogger(__name__)


def filter_dict_by_prefix(source, prefix):
    result = dict()
    for key in source.keys():
        if key.startswith(prefix):
            key_stripped = key[len(prefix) :]
            result[key_stripped] = source[key]
    return result


class Learner(BaseEstimator, metaclass=ABCMeta):
    def _store_kwargs(self, kwargs, allowed_prefixes):
        """Store kwargs with whitelisted prefixes into this objects attributes.

        Raises an exception if one of the kwargs does not match a whiltelisted prefix.
        """
        self.allowed_prefixes_ = allowed_prefixes

        def starts_with_legal_prefix(key):
            for prefix in allowed_prefixes:
                if key.startswith(prefix):
                    return True
            return False

        for key in kwargs.keys():
            if not starts_with_legal_prefix(key):
                raise TypeError(
                    f"'__init__() got an unexpected keyword argument '{key}'. Allowed prefixes: {allowed_prefixes}."
                )

        vars(self).update(kwargs)

    def _get_prefix_attributes(self, prefix):
        """Return all attributes of this class that start with a given prefix.

        The prefix is stripped in the result. This can be used to pass on some
        parameters to subclasses.
        """
        return filter_dict_by_prefix(self.__dict__, prefix)

    def _initialize_optimizer(self):
        optimizer_params = self._get_prefix_attributes("optimizer__")
        self.optimizer_ = self.optimizer(**optimizer_params)

    def _initialize_regularizer(self):
        regularizer_params = self._get_prefix_attributes("kernel_regularizer__")
        if self.kernel_regularizer is not None:
            self.kernel_regularizer_ = self.kernel_regularizer(**regularizer_params)
        else:
            # No regularizer is an option.
            logger.warning("You specified regularizer parameters but no regularizer.")
            self.kernel_regularizer_ = None

    def set_params(self, **params):
        """Set a hyper-paramter for this learner.

        Accepts the same parameters as __init__.
        """
        legal_parameters = self.get_params().keys()
        for param in params.keys():
            if param not in legal_parameters:
                raise TypeError(
                    f"Unexpected parameter for {type(self).__name__}: `{param}.` Legal parameters are {set(legal_parameters)}."
                )
        vars(self).update(params)

    def _prefix_to_class_mapping(self):
        """Map nested parameter prefixes to the classes they are passed to.

        Necessary for get_params.
        """
        result = dict()
        allowed_prefixes = (
            self.allowed_prefixes_ if hasattr(self, "allowed_prefixes_") else []
        )
        for prefix in allowed_prefixes:
            base_parameter = prefix[:-2]  # prefixes always end with two underscores
            if hasattr(self, base_parameter):
                result[prefix] = vars(self)[base_parameter]
            # This is a hack to work with our common "hidden_dense_layer__"
            # arguments. They do not correspond to a single hidden_dense_layer
            # attribute. They are passed to all hidden dense layers that are
            # part of the network. Therefore we just hardcode the "Dense" class
            # for them.
            elif base_parameter == "hidden_dense_layer":
                result[prefix] = Dense
            else:
                raise ValueError(
                    f"Prefix {prefix} could not be associated to any class."
                )
        return result

    def get_params(self, deep=True):
        """Return all hyperparmeters of this learner.

        Limitation: This does not recurse into parameters, so it only works for a
        single layer.

        Parameters
        ----------
        deep: bool, default=True
            Whether or not to return parameters of subobjects as well. Support
            for this is currently limited, so parameters of subobjects are
            returned on a best-effort basis if they were passed with the
            subobject__parameter convention.

        Returns
        -------
        dict
            A dictionary of parameters.
        """
        # Get all the regular parameters form BaseEstimator.
        result = super().get_params()

        if not deep:
            return result

        # Handle the parameter that could be passed to uninitialized subclasses
        # (optimizer__lr etc.).
        parameters_for_prefix = dict()
        for (prefix, base_class) in self._prefix_to_class_mapping().items():
            parameters_for_prefix = dict()
            signature = inspect.signature(base_class)
            for parameter in signature.parameters:
                if signature.parameters[parameter].default != inspect._empty:
                    parameters_for_prefix[parameter] = signature.parameters[
                        parameter
                    ].default
            # Override with explicitly set parameter values
            parameters_for_prefix.update(self._get_prefix_attributes(prefix))
            for (arg, default) in parameters_for_prefix.items():
                result[prefix + arg] = default

        return result

    @abstractmethod
    def fit(self, X, Y, **kwargs):
        """
        Fit the preference learning algorithm on the provided set of queries X and preferences Y of those objects.
        The provided queries and corresponding preferences are of a fixed size (numpy arrays).

        Parameters
        ----------
        X : array-like, shape (n_samples, n_objects, n_features)
            Feature vectors of the objects
        Y : array-like, shape (n_samples, n_objects)
            Preferences of the objects in form of rankings or choices
        """
        raise NotImplementedError

    def _pre_fit(self):
        """Perform stateful initialization before fitting.

        This function is for initialization that does not depend on the data,
        but still requires some processing and therefore should not happen in
        __init__. Examples include initialization of optimizers, construction
        of NeuralNetwork layers (if it can be done without knowledge of the
        data) etc.

        You should always call this function before fit, even if you do not
        override it. If you override it, you should call the super method first
        so that general initializations can be inherited.
        """
        pass

    @abstractmethod
    def _predict_scores_fixed(self, X, **kwargs):
        """
        Predict the scores for a given collection of sets of objects of same size.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_objects, n_features)


        Returns
        -------
        Y : array-like, shape (n_samples, n_objects)
            Returns the scores of each of the objects for each of the samples.
        """
        raise NotImplementedError

    def predict_for_scores(self, scores, **kwargs):
        raise NotImplementedError

    def predict_scores(self, X, **kwargs):
        """
        Predict the utility scores for each object in the collection of set of objects called a query set.

        Parameters
        ----------
        X : dict or numpy array
            Dictionary with a mapping from query set size to numpy arrays or a single numpy array of size:
            (n_instances, n_objects, n_features)

        Returns
        -------
        Y : dict or numpy array
            Dictionary with a mapping from query set size to numpy arrays or a single numpy array of size:
            (n_instances, n_objects)
        """
        logger.info("Predicting scores")

        if isinstance(X, dict):
            scores = dict()
            for ranking_size, x in X.items():
                n_instances, n_objects, n_features = x.shape
                scores[ranking_size] = self._predict_scores_fixed(x, **kwargs)

        else:
            scores = self._predict_scores_fixed(X, **kwargs)
        return scores

    def predict(self, X, **kwargs):
        """
        Predict preferences in the form of rankings or choices for a given collection of sets of objects called
        a query set using the function :meth:`.predict_for_scores`.

        Parameters
        ----------
        X : dict or numpy array
            Dictionary with a mapping from the query set size to numpy arrays or a single numpy array of size:
            (n_instances, n_objects, n_features)


        Returns
        -------
        Y : dict or numpy array
            Dictionary with a mapping from the query set size to numpy arrays or a single numpy array containing
            predicted preferences of size:
            (n_instances, n_objects)
        """
        logger.debug("Predicting started")

        scores = self.predict_scores(X, **kwargs)
        logger.debug("Predicting scores complete")

        return self.predict_for_scores(scores, **kwargs)

    def __call__(self, X, *args, **kwargs):
        """
        Predict preferences in the form of rankings or choices for a given collection of sets of objects.

        Parameters
        ----------
        X : dict or numpy array
            Dictionary with a mapping from ranking size to numpy arrays
            or a single numpy array of size:
            (n_instances, n_objects, n_features)


        Returns
        -------
        Y : Dictionary with a mapping from ranking size to numpy arrays
            or a single numpy array of size:
            (n_instances, n_objects)
            Predicted ranking
        """
        return self.predict(X, **kwargs)

    @classmethod
    def __subclasshook__(cls, C):
        if cls is Learner:
            has_fit = any("fit" in B.__dict__ for B in C.__mro__)
            has_predict = any("predict" in B.__dict__ for B in C.__mro__)
            has_scores = any("predict_scores" in B.__dict__ for B in C.__mro__)
            has_scores_fixed = any(
                "_predict_scores_fixed" in B.__dict__ for B in C.__mro__
            )
            has_predict_for_scores = any(
                "predict_for_scores" in B.__dict__ for B in C.__mro__
            )
            if (
                has_fit
                and has_predict
                and has_scores
                and has_scores_fixed
                and has_predict_for_scores
            ):
                return True
        return NotImplemented
