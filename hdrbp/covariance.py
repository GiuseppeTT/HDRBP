import logging
from abc import ABC, abstractmethod

import numpy as np

from hdrbp._util import (
    FLOAT_RESOLUTION,
    basic_repr,
    basic_str,
    build_covariances,
    demean,
    enforce_sum_one,
    extract_correlations,
    extract_upper_elements,
    extract_volatilities,
)

logger = logging.getLogger(__name__)


@basic_str
@basic_repr
class CovarianceEstimator(ABC):
    def __hash__(self) -> int:
        return hash(repr(self))

    def estimate(self, returns: np.ndarray, is_demeaned: bool = False) -> np.ndarray:
        logger.debug(f"{self}: Estimating covariances")

        if is_demeaned:
            covariances = self._demeaned_estimate(returns)
        else:
            covariances = self._meaned_estimate(returns)

        return covariances

    def _meaned_estimate(self, returns):
        demeaned_returns = demean(returns, axis=0, keepdims=True)
        covariances = self._demeaned_estimate(demeaned_returns)

        return covariances

    @abstractmethod
    def _demeaned_estimate(self, returns):
        pass


class EqualVariance(CovarianceEstimator):
    def _demeaned_estimate(self, returns):
        sample_covariances = SampleCovariance().estimate(returns, is_demeaned=True)
        sample_variances = np.diag(sample_covariances)

        _, asset_count = returns.shape
        global_variance = np.mean(sample_variances)
        covariances = global_variance * np.eye(asset_count)

        return covariances


class ZeroCorrelation(CovarianceEstimator):
    def _demeaned_estimate(self, returns):
        sample_covariances = SampleCovariance().estimate(returns, is_demeaned=True)
        covariances = np.diag(np.diag(sample_covariances))

        return covariances


class EqualCorrelation(CovarianceEstimator):
    def _demeaned_estimate(self, returns):
        sample_covariances = SampleCovariance().estimate(returns, is_demeaned=True)
        sample_volatilities = extract_volatilities(sample_covariances)
        sample_correlations = extract_correlations(sample_covariances)

        volatilities = sample_volatilities

        _, asset_count = returns.shape
        global_correlation = np.mean(extract_upper_elements(sample_correlations))
        # fmt: off
        correlations = (
            (1 - global_correlation) * np.eye(asset_count)
            + global_correlation * np.ones((asset_count, asset_count))
        )
        # fmt: on

        covariances = build_covariances(volatilities, correlations)

        return covariances


class SampleCovariance(CovarianceEstimator):
    def _demeaned_estimate(self, returns):
        time_count, _ = returns.shape
        covariances = returns.T @ returns / time_count

        return covariances


class RiskMetrics1994(CovarianceEstimator):
    # RiskMetrics - Technical Document
    def __init__(self) -> None:
        self._smooth = 0.94

    def _demeaned_estimate(self, returns):
        return _apply_ewma(returns, self._smooth)


class RiskMetrics2006(CovarianceEstimator):
    # The RiskMetrics 2006 methodology
    def __init__(self) -> None:
        # https://en.wikipedia.org/wiki/Exponential_decay

        self._min_mean_lifetime = 4
        self._mean_lifetime_ratio = np.sqrt(2)
        self._mean_lifetime_count = 14
        self._mixture_decay = 1560

        exponents = np.arange(self._mean_lifetime_count)
        mean_lifetimes = self._min_mean_lifetime * self._mean_lifetime_ratio ** exponents
        smooths = np.exp(-1 / mean_lifetimes)
        mixture_weights = enforce_sum_one(1 - np.log(mean_lifetimes) / np.log(self._mixture_decay))

        self._smooths = smooths
        self._mixture_weights = mixture_weights

    def _demeaned_estimate(self, returns):
        return _apply_mixture_ewma(returns, self._smooths, self._mixture_weights)


# TODO: Move comments to RiskMetrics1994 documentation
def _apply_mixture_ewma(returns, smooths, mixture_weights):
    # mixture_ewma[:, :, time + 1] = (
    #     mixture_weights[0] * ewma[:, :, time + 1, 0]
    #     + ...
    #     + mixture_weights[ewma_count - 1] * ewma[:, :, time + 1, ewma_count - 1]
    # )
    #
    # mixture_ewma[:, :, time + 1] = (
    #     weights[0] * returns[0, :] @ returns[0, :].T
    #     + ...
    #     + weights[time] * returns[time, :] @ returns[time, :].T
    # )
    #
    # weights[time] = (
    #     mixture_weights[0] + ewma_weights[time, 0]
    #     + ...
    #     + mixture_weights[ewma_count - 1] + ewma_weights[time, ewma_count - 1]
    # )

    time_count, _ = returns.shape

    max_smooth = np.max(smooths)
    term_count = _count_ewma_terms(max_smooth, max_count=time_count)
    times = np.arange(term_count)
    times = times[:, np.newaxis]

    ewma_weights = enforce_sum_one(smooths ** (time_count - times), axis=0)
    weights = np.einsum("ij, j -> i", ewma_weights, mixture_weights)

    returns = returns[-term_count:, :]
    mixture_ewma = np.einsum("ki, kj, k -> ij", returns, returns, weights)

    return mixture_ewma


# TODO: Move comments to RiskMetrics1994 documentation
def _apply_ewma(returns: np.ndarray, smooth: float) -> np.ndarray:
    # ewma[:, :, time + 1] = (
    #     smooth * ewma[:, :, time]
    #     + (1 - smooth) * returns[time, :] @ returns[time, :].T
    # )
    #
    # ewma[:, :, time + 1] = (
    #     weights[0] * returns[0, :] @ returns[0, :].T
    #     + ...
    #     + weights[time] * returns[time, :] @ returns[time, :].T
    # )
    #
    # weights[time] = (1 - smooth) * smooth ** (time_count - time)

    time_count, _ = returns.shape

    term_count = _count_ewma_terms(smooth, max_count=time_count)
    times = np.arange(term_count)
    weights = enforce_sum_one(smooth ** (time_count - times))

    returns = returns[-term_count:, :]
    ewma = np.einsum("ki, kj, k -> ij", returns, returns, weights)

    return ewma


# TODO: Move comments to RiskMetrics1994 documentation
def _count_ewma_terms(
    smooth: float,
    max_count: float = np.inf,
    numerical_error: float = FLOAT_RESOLUTION,
) -> int:
    # weight[index] = (1 - smooth) * smooth ** index
    #
    # weights[0] + ... weights[term_count - 1] = 1 - smooth ** term_count
    #
    # weights[term_count] + weights[term_count + 1] ... = smooth ** term_count
    #
    # smooth ** term_count <= numerical_error
    #
    # term_count = ceil( log(numerical_error) / log(smooth) )

    term_count = np.log(numerical_error) / np.log(smooth)
    term_count = np.ceil(term_count)
    term_count = min(term_count, max_count)
    term_count = int(term_count)

    return term_count


class LinearShrinkage(CovarianceEstimator):
    # https://doi.org/10.1016/S0047-259X(03)00096-4
    def _demeaned_estimate(self, returns):
        sample_covariances = SampleCovariance().estimate(returns, is_demeaned=True)
        target_covariances = EqualVariance().estimate(returns, is_demeaned=True)
        shrinkage = _estimate_shrinkage(returns, sample_covariances, target_covariances)

        covariances = (1 - shrinkage) * sample_covariances + shrinkage * target_covariances

        return covariances


# TODO: find better names for d_squared and b_squared
def _estimate_shrinkage(returns, sample_covariances, target_covariances):
    d_squared = _compute_squared_norm(sample_covariances - target_covariances)

    time_count, _ = returns.shape
    b_squared = 0
    for row in returns:
        b_squared += _compute_squared_norm(np.outer(row, row) - sample_covariances)
    b_squared = b_squared / time_count ** 2
    b_squared = min(b_squared, d_squared)

    shrinkage = b_squared / d_squared

    return shrinkage


def _compute_squared_norm(matrix):
    # squared_norm = (
    #     matrix[0, 0] ** 2 + ... + matrix[0, column_count - 1] ** 2
    #     + ...
    #     + matrix[row_count - 1, 0] ** 2 + ... + matrix[row_count - 1, column_count - 1] ** 2
    # )

    _, column_count = matrix.shape
    squared_norm = np.einsum("ij, ij", matrix, matrix) / column_count

    return squared_norm


class NonLinearShrinkage(CovarianceEstimator):
    def _demeaned_estimate(self, returns):
        raise NotImplementedError


# TODO
# TODO: choose a better name?
# TODO: merge CCC and DCC into one class?
class GARCHCovariance(CovarianceEstimator, ABC):
    def _demeaned_estimate(self, returns):
        raise NotImplementedError


class ConstantConditionalCorrelation(GARCHCovariance):
    pass


class DynamicConditionalCorrelation(GARCHCovariance):
    pass
