import itertools
import json
import logging
import os
import pickle
from argparse import ArgumentParser
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from datasets import load_dataset
from scipy import stats
from scipy.stats import pearsonr as pearson_correlation
from scipy.stats import spearmanr as spearman_correlation
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score, average_precision_score, log_loss
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from tqdm import tqdm

# Repo - https://github.com/jettify/xicorrelation
from xicorrelation import xicorr as xi_correlation

from model_parameters import (
    CATEGORICAL_FEATURE_COLUMNS,
    CONTINUOUS_FEATURE_COLUMNS,
    DATA_SCHEME,
    EXPERIMENT_BASE,
    FIT_INTERCEPT,
    GLOBAL_SEED,
    MAX_MODEL_ITERATIONS,
    MODEL_SIZE,
    GENERATION_HF_DATASET_NAME,
    ENTROPY_HF_DATASET_NAME,
    REG_NAME,
    REG_STRENGTH,
    SEQUENCE_DUPLICATE_THRESHOLD,
    TAXONOMIES,
    TAXONOMY_QUANTILES,
    TAXONOMY_SEARCH_FEATURES,
    TEST_SIZE,
    TRAIN_SIZE,
    VALIDATION_SIZE,
    derive_is_templating_feature,
)

LOGGER = logging.getLogger("experiments")
LOGGER.setLevel(logging.INFO)


@dataclass
class BaselineModelResult:
    model: LogisticRegression
    test_predictions: list
    roc_auc: float
    pr_auc: float
    wald_stats: float
    wald_pvalue: float


@dataclass
class TaxonomicalModelResult:
    model: LogisticRegression
    test_predictions: list
    roc_auc: float
    pr_auc: float
    wald_stats: float
    wald_pvalue: float
    lrt_pvalue: float


def parse_cli_args():
    """
    Parse the command line arguments for the script.
    """
    parser = ArgumentParser()

    run_id_args_help = "The ID for this run. Defaults to current date and time."
    run_id_args_default = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    parser.add_argument(
        "--run_id",
        type=str,
        help=run_id_args_help,
        default=run_id_args_default,
    )

    return parser.parse_args()


def load_hf_dataset():
    pile_generation_dataset = load_dataset(GENERATION_HF_DATASET_NAME, split=f"pile_{DATA_SCHEME}_{MODEL_SIZE}").to_pandas()
    pile_entropy_dataset = load_dataset(ENTROPY_HF_DATASET_NAME, revision=f"{DATA_SCHEME}_{MODEL_SIZE}", split="pile").to_pandas()
    memories_generation_dataset = load_dataset(GENERATION_HF_DATASET_NAME, split=f"memories_{DATA_SCHEME}_{MODEL_SIZE}").to_pandas()
    memories_entropy_dataset = load_dataset(ENTROPY_HF_DATASET_NAME, revision=f"{DATA_SCHEME}_{MODEL_SIZE}", split="memories").to_pandas()

    LOGGER.info(f"Pile generation dataset shape: {pile_generation_dataset.shape}")
    LOGGER.info(f"Pile entropy dataset shape: {pile_entropy_dataset.shape}")
    LOGGER.info(f"Memories generation dataset shape: {memories_generation_dataset.shape}")
    LOGGER.info(f"Memories entropy dataset shape: {memories_entropy_dataset.shape}")

    LOGGER.info("Merging generation and entropy datasets...")
    pile_dataset = pd.merge(
        left=pile_generation_dataset,
        right=pile_entropy_dataset,
        on="sequence_id",
        # Some metrics are overlapping, suffixing them to differentiate the data source
        suffixes=("_generation", "_entropy"),
    )
    memories_dataset = pd.merge(
        left=memories_generation_dataset,
        right=memories_entropy_dataset,
        on="sequence_id",
        suffixes=("_generation", "_entropy"),
    )

    LOGGER.info(f"Merged pile dataset shape: {pile_dataset.shape}")
    LOGGER.info(f"Merged memories dataset shape: {memories_dataset.shape}")

    return pile_dataset, memories_dataset


def construct_derived_features(pile_dataset, memories_dataset):
    LOGGER.info("Constructing `is_templating` feature...")
    pile_dataset["is_templating"] = pile_dataset.apply(derive_is_templating_feature, axis=1)
    memories_dataset["is_templating"] = memories_dataset.apply(derive_is_templating_feature, axis=1)

    return pile_dataset, memories_dataset


def get_taxonomy_function():
    def classify_row(row):
        if row.sequence_duplicates > SEQUENCE_DUPLICATE_THRESHOLD:
            return "recitation"
        if row.is_templating:
            return "reconstruction"

        return "recollection"

    return classify_row


def normalize_dataset(pile_dataset):
    # Scale the continuous features to be zero-mean and unit-variance
    feature_scaler = StandardScaler().fit(pile_dataset[CONTINUOUS_FEATURE_COLUMNS])
    scaled_continuous_features = feature_scaler.transform(pile_dataset[CONTINUOUS_FEATURE_COLUMNS])
    categorical_features = pile_dataset[CATEGORICAL_FEATURE_COLUMNS].values

    features = np.hstack((scaled_continuous_features, categorical_features))
    labels = (pile_dataset.memorization_score == 1.0).astype(int).values

    return features, labels


def split_dataset(features, labels):
    indices = np.arange(len(features))

    train_features, remain_features, train_labels, remain_labels = train_test_split(
        features,
        labels,
        test_size=1 - TRAIN_SIZE,
        random_state=GLOBAL_SEED,
    )

    validation_features, test_features, validation_labels, test_labels = train_test_split(
        remain_features,
        remain_labels,
        test_size=TEST_SIZE / (TEST_SIZE + VALIDATION_SIZE),
        random_state=GLOBAL_SEED,
    )

    train_indices, remain_indices = train_test_split(
        indices,
        test_size=1 - TRAIN_SIZE,
        random_state=GLOBAL_SEED,
    )

    validation_indices, test_indices = train_test_split(
        remain_indices,
        test_size=TEST_SIZE / (TEST_SIZE + VALIDATION_SIZE),
        random_state=GLOBAL_SEED,
    )

    return (
        (train_features, train_labels, train_indices),
        (validation_features, validation_labels, validation_indices),
        (test_features, test_labels, test_indices),
    )


def likelihood_ratio_test(baseline_predictions, taxonomical_predictions, labels, dof=1):
    # Reference: https://stackoverflow.com/questions/48185090/how-to-get-the-log-likelihood-for-a-logistic-regression-model-in-sklearn
    # H_0 (Null Hypothesis)
    baseline_log_likelihood = -log_loss(labels, baseline_predictions, normalize=False)
    # H_1 (Alternative Hypothesis)
    taxonomical_log_likelihood = -log_loss(labels, taxonomical_predictions, normalize=False)

    # References
    # - https://stackoverflow.com/questions/38248595/likelihood-ratio-test-in-python
    # - https://rnowling.github.io/machine/learning/2017/10/07/likelihood-ratio-test.html
    likelihood_ratio = -2 * (baseline_log_likelihood - taxonomical_log_likelihood)
    pvalue = stats.chi2.sf(likelihood_ratio, df=dof)

    return pvalue


def wald_test(model, features):
    probs = model.predict_proba(features)
    num_samples = len(probs)
    # Include the intercept term as the first feature
    num_features = len(model.coef_[0]) + 1
    coefficients = np.concatenate([model.intercept_, model.coef_[0]])
    full_features = np.matrix(np.insert(np.array(features), 0, 1, axis=1))

    # References
    # - https://stackoverflow.com/questions/25122999/scikit-learn-how-to-check-coefficients-significance
    ans = np.zeros((num_features, num_features))
    for i in range(num_samples):
        ans = ans + np.dot(np.transpose(full_features[i, :]), full_features[i, :]) * probs[i, 1] * probs[i, 0]
    var_covar_matrix = np.linalg.inv(np.matrix(ans))
    std_err = np.sqrt(np.diag(var_covar_matrix))
    wald = coefficients**2 / std_err**2
    pvalues = np.array([2 * (1 - stats.norm.cdf(np.abs(w))) for w in np.sqrt(wald)])

    return wald, pvalues


def calculate_correlation_coefficients(features, labels):
    pearsons, spearmans, xis = [], [], []

    for i in range(features.shape[1]):
        LOGGER.info(f"Calculating correlation coefficients on feature index {i}...")
        feature = features[:, i]
        pearson_result = pearson_correlation(feature, labels, alternative="two-sided")
        spearman_result = spearman_correlation(feature, labels, alternative="two-sided")
        xi_result = xi_correlation(feature, labels)

        pearsons.append((pearson_result.statistic, pearson_result.pvalue))
        spearmans.append((spearman_result.statistic, spearman_result.pvalue))
        xis.append((xi_result.correlation, xi_result.pvalue))

    return pearsons, spearmans, xis


def calculate_all_correlation_coefficients(features, labels, taxonomy_categories):
    coefficients = defaultdict(dict)

    baseline_pearson, baseline_spearman, baseline_xi = calculate_correlation_coefficients(features, labels)
    coefficients["baseline"]["pearson"] = baseline_pearson
    coefficients["baseline"]["spearman"] = baseline_spearman
    coefficients["baseline"]["xi"] = baseline_xi

    for taxonomy in TAXONOMIES:
        taxonomical_feature = np.expand_dims((taxonomy_categories == taxonomy).astype(int).values, axis=1)
        taxonomical_pearson, taxonomical_spearman, taxonomical_xi = calculate_correlation_coefficients(taxonomical_feature, labels)
        coefficients[taxonomy]["pearson"] = taxonomical_pearson
        coefficients[taxonomy]["spearman"] = taxonomical_spearman
        coefficients[taxonomy]["xi"] = taxonomical_xi

    return coefficients


def save_correlation_coefficients(
    base_path,
    data_scheme,
    model_size,
    coefficients,
):
    full_path = f"{base_path}/{data_scheme}/{model_size}"

    if not os.path.exists(full_path):
        os.makedirs(full_path)

    metadata_path = os.path.join(full_path, "correlation_coefficients.json")
    with open(metadata_path, "w") as file:
        json.dump(coefficients, file)


def save_lr_models(
    base_path,
    data_scheme,
    taxonomy,
    model_size,
    model,
    model_metadata,
):
    full_path = f"{base_path}/{data_scheme}/{model_size}/{taxonomy}"

    if not os.path.exists(full_path):
        os.makedirs(full_path)

    model_path = os.path.join(full_path, "lr.pkl")
    with open(model_path, "wb") as file:
        pickle.dump(model, file)

    metadata = {
        "data_scheme": data_scheme,
        "taxonomy": taxonomy,
        "model_size": model_size,
        **model_metadata,
    }

    metadata_path = os.path.join(full_path, "metadata.json")
    with open(metadata_path, "w") as file:
        json.dump(metadata, file)


def train_lr_model(train_features, train_labels, test_features, test_labels):
    # Training with fixed parameters
    model = LogisticRegression(
        fit_intercept=FIT_INTERCEPT,
        random_state=GLOBAL_SEED,
        max_iter=MAX_MODEL_ITERATIONS,
        penalty=REG_NAME,
        C=REG_STRENGTH,
    )
    model.fit(train_features, train_labels)

    # Calculate classification metrics
    test_predictions = model.predict_proba(test_features)[:, 1]
    roc_auc = roc_auc_score(test_labels, test_predictions)
    pr_auc = average_precision_score(test_labels, test_predictions)
    LOGGER.info(f"ROC AUC: {roc_auc:.4f} | PR AUC: {pr_auc:.4}")

    return model, test_predictions, roc_auc, pr_auc


def train_baseline_model(train_features, train_labels, test_features, test_labels) -> BaselineModelResult:
    model, test_predictions, roc_auc, pr_auc = train_lr_model(train_features, train_labels, test_features, test_labels)

    try:
        LOGGER.info("Performing Wald Test...")
        wald_stats, wald_pvalue = wald_test(model, test_features)
    except Exception as e:
        LOGGER.info(f"Wald Test failed with Exception {e}")
        wald_stats, wald_pvalue = None, None

    return BaselineModelResult(model, test_predictions, roc_auc, pr_auc, wald_stats, wald_pvalue)


def train_taxonomical_model(
    train_features,
    train_labels,
    test_features,
    test_labels,
    baseline_test_predictions,
) -> TaxonomicalModelResult:
    model, test_predictions, roc_auc, pr_auc = train_lr_model(train_features, train_labels, test_features, test_labels)

    lrt_pvalue = None
    wald_stats, wald_pvalue = None, None

    LOGGER.info("Performing Likelihood Ratio Test...")
    lrt_pvalue = likelihood_ratio_test(baseline_test_predictions, test_predictions, test_labels)

    try:
        LOGGER.info("Performing Wald Test...")
        wald_stats, wald_pvalue = wald_test(model, test_features)
    except Exception as e:
        LOGGER.info(f"Wald Test failed with Exception {e}")

    return TaxonomicalModelResult(model, test_predictions, roc_auc, pr_auc, wald_stats, wald_pvalue, lrt_pvalue)


def train_baseline_and_taxonomic_models(
    train_features,
    train_labels,
    train_indices,
    test_features,
    test_labels,
    test_indices,
    taxonomy_categories,
):
    LOGGER.info("Training the baseline model...")
    baseline_result = train_baseline_model(train_features, train_labels, test_features, test_labels)

    LOGGER.info("Saving baseline model results...")
    baseline_metadata = {
        "roc_auc": baseline_result.roc_auc,
        "pr_auc": baseline_result.pr_auc,
        # Not applicable since there are no alternative models to compare with, this is the baseline
        "lrt_pvalue": None,
        "wald_statistic": list(baseline_result.wald_stats),
        "wald_pvalue": list(baseline_result.wald_pvalue),
    }
    save_lr_models(EXPERIMENT_BASE, DATA_SCHEME, "baseline", MODEL_SIZE, baseline_result.model, baseline_metadata)

    for taxonomy in TAXONOMIES:
        taxonomical_feature = np.expand_dims((taxonomy_categories == taxonomy).astype(int).values, axis=1)
        train_taxonomical_features = np.hstack((train_features, taxonomical_feature[train_indices]))
        test_taxonomical_features = np.hstack((test_features, taxonomical_feature[test_indices]))

        LOGGER.info(f"Training {taxonomy} model...")
        taxonomical_model_result = train_taxonomical_model(
            train_taxonomical_features,
            train_labels,
            test_taxonomical_features,
            test_labels,
            baseline_result.test_predictions,
        )

        LOGGER.info(f"Saving {taxonomy} model results...")
        taxonomical_model_metadata = {
            "roc_auc": taxonomical_model_result.roc_auc,
            "pr_auc": taxonomical_model_result.pr_auc,
            "lrt_pvalue": taxonomical_model_result.lrt_pvalue,
            "wald_statistic": list(taxonomical_model_result.wald_stats) if taxonomical_model_result.wald_stats is not None else [],
            "wald_pvalue": list(taxonomical_model_result.wald_pvalue) if taxonomical_model_result.wald_pvalue is not None else [],
        }
        save_lr_models(EXPERIMENT_BASE, DATA_SCHEME, taxonomy, MODEL_SIZE, taxonomical_model_result.model, taxonomical_model_metadata)

    return baseline_result


def save_taxonomy_search_models(
    base_path,
    data_scheme,
    model_size,
    taxonomy_1_name,
    taxonomy_1_threshold_quantile,
    taxonomy_2_name,
    taxonomy_2_threshold_quantile,
    model,
    model_metadata,
):
    full_path = f"{base_path}/{data_scheme}/{model_size}/taxonomy_search/{taxonomy_1_name}-{taxonomy_1_threshold_quantile}/{taxonomy_2_name}-{taxonomy_2_threshold_quantile}"

    if not os.path.exists(full_path):
        os.makedirs(full_path)

    model_path = os.path.join(full_path, "lr.pkl")
    with open(model_path, "wb") as file:
        pickle.dump(model, file)

    metadata = {
        "data_scheme": data_scheme,
        "model_size": model_size,
        "taxonomy_1_feature_name": taxonomy_1_name,
        "taxonomy_1_threshold_quantile": taxonomy_1_threshold_quantile,
        "taxonomy_2_feature_name": taxonomy_2_name,
        "taxonomy_2_threshold_quantile": taxonomy_2_threshold_quantile,
        **model_metadata,
    }

    metadata_path = os.path.join(full_path, "metadata.json")
    with open(metadata_path, "w") as file:
        json.dump(metadata, file)


def generate_taxonomy_quantile_thresholds(memories_dataset):
    taxonomy_thresholds = defaultdict(dict)

    for feature in tqdm(TAXONOMY_SEARCH_FEATURES):
        for quantile in TAXONOMY_QUANTILES:
            threshold = memories_dataset[feature].quantile(quantile)
            taxonomy_thresholds[feature][quantile] = threshold

    LOGGER.info(f"Taxonomy Search Quantile Thresholds {taxonomy_thresholds}")

    return taxonomy_thresholds


def generate_optimal_taxonomy_candidate(feature_1, threshold_1, feature_2, threshold_2):
    def classify_row(row):
        has_taxonomy_1 = row[feature_1] > threshold_1
        has_taxonomy_2 = row[feature_2] > threshold_2

        if has_taxonomy_1:
            return "taxonomy_1"

        if has_taxonomy_2:
            return "taxonomy_2"

        return "taxonomy_3"

    return classify_row


def train_all_taxonomy_pairs(
    train_features,
    train_labels,
    train_indices,
    validation_features,
    validation_labels,
    validation_indices,
    baseline_model,
    taxonomy_thresholds,
    pile_dataset,
):
    LOGGER.info("Generating all pairs of features for the optimal taxonomy")
    features_with_quantiles = list(itertools.product(TAXONOMY_SEARCH_FEATURES, TAXONOMY_QUANTILES))
    # We drop candidates where they are the same feature.
    # Future work may include exploring features with interesting value regimes.
    optimal_taxonomy_candidates = [t for t in list(itertools.permutations(features_with_quantiles, 2)) if t[0][0] != t[1][0]]
    LOGGER.info(f"Training {len(optimal_taxonomy_candidates)} model(s) to find the optimal taxonomy")

    LOGGER.info("Getting baseline validation predictions...")
    baseline_validation_predictions = baseline_model.predict_proba(validation_features)[:, 1]

    for i, (candidate_1, candidate_2) in enumerate(optimal_taxonomy_candidates):
        candidate_1_name, candidate_1_threshold_quantile = candidate_1
        candidate_2_name, candidate_2_threshold_quantile = candidate_2
        candidate_1_threshold = taxonomy_thresholds[candidate_1_name][candidate_1_threshold_quantile]
        candidate_2_threshold = taxonomy_thresholds[candidate_2_name][candidate_2_threshold_quantile]
        LOGGER.info(f"Training [{i + 1}/{len(optimal_taxonomy_candidates)}] taxonomy candidate model...")
        LOGGER.info(f"Candidate 1 feature - {candidate_1_name} | Candidate 2 feature - {candidate_2_name}")
        LOGGER.info(f"Candidate 1 value - {candidate_1_threshold} | Candidate 2 value - {candidate_2_threshold}")

        LOGGER.info("Generating taxonomy categories...")
        taxonomy_func = generate_optimal_taxonomy_candidate(candidate_1_name, candidate_1_threshold, candidate_2_name, candidate_2_threshold)
        taxonomy_categories = pile_dataset.apply(taxonomy_func, axis=1)

        for taxonomy in ["taxonomy_1", "taxonomy_2", "taxonomy_3"]:
            taxonomical_feature = np.expand_dims((taxonomy_categories == taxonomy).astype(int).values, axis=1)
            train_taxonomical_features = np.hstack((train_features, taxonomical_feature[train_indices]))
            validation_taxonomical_features = np.hstack((validation_features, taxonomical_feature[validation_indices]))

            LOGGER.info(f"Training {taxonomy} model...")
            taxonomical_model_result = train_taxonomical_model(
                train_taxonomical_features,
                train_labels,
                validation_taxonomical_features,
                validation_labels,
                baseline_validation_predictions,
            )

            LOGGER.info(f"Saving {taxonomy} model results...")
            taxonomical_model_metadata = {
                "roc_auc": taxonomical_model_result.roc_auc,
                "pr_auc": taxonomical_model_result.pr_auc,
                "lrt_pvalue": taxonomical_model_result.lrt_pvalue,
                "wald_statistic": list(taxonomical_model_result.wald_stats) if taxonomical_model_result.wald_stats is not None else [],
                "wald_pvalue": list(taxonomical_model_result.wald_pvalue) if taxonomical_model_result.wald_pvalue is not None else [],
            }
            save_taxonomy_search_models(
                EXPERIMENT_BASE,
                DATA_SCHEME,
                MODEL_SIZE,
                candidate_1_name,
                candidate_1_threshold_quantile,
                candidate_2_name,
                candidate_2_threshold_quantile,
                taxonomical_model_result.model,
                taxonomical_model_metadata,
            )


def main():
    """
    The main function of the script.
    """
    args = parse_cli_args()

    os.makedirs(f"./{EXPERIMENT_BASE}/{args.run_id}", exist_ok=True)
    file_handler = logging.FileHandler(f"./{EXPERIMENT_BASE}/{args.run_id}/run.log")
    file_handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(module)s:%(lineno)d - %(message)s")
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    LOGGER.info("---------------------------------------------------------------------------")
    LOGGER.info("Starting model training with the following parameters:")
    LOGGER.info(f"Run ID: {args.run_id}")
    LOGGER.info("---------------------------------------------------------------------------")

    LOGGER.info("Loading HF datasets...")
    pile_dataset, memories_dataset = load_hf_dataset()

    LOGGER.info("Constructing derived features...")
    pile_dataset, memories_dataset = construct_derived_features(pile_dataset, memories_dataset)

    LOGGER.info("Generating taxonomy categories for each sample...")
    taxonomy_categories = pile_dataset.apply(get_taxonomy_function(), axis=1)

    features, labels = normalize_dataset(pile_dataset)
    prob_negatives = (labels == 0).astype(int).sum() / len(labels)
    prob_positives = 1 - prob_negatives

    LOGGER.info("Class Priors")
    LOGGER.info(f"Memorized (+): {prob_positives * 100:.4f}%")
    LOGGER.info(f"Non-memorized (-): {prob_negatives * 100:.4f}%")
    LOGGER.info("=" * 30)

    LOGGER.info(f"Split datasets into {TRAIN_SIZE * 100}% training, {VALIDATION_SIZE * 100}% validation, and {TEST_SIZE * 100}% test")
    (train, validation, test) = split_dataset(features, labels)

    LOGGER.info("Calculating correlation coefficients of base + taxonomic features...")
    correlation_results = calculate_all_correlation_coefficients(features, labels, taxonomy_categories)
    save_correlation_coefficients(EXPERIMENT_BASE, DATA_SCHEME, MODEL_SIZE, correlation_results)

    (train_features, train_labels, train_indices) = train
    (validation_features, validation_labels, validation_indices) = validation
    (test_features, test_labels, test_indices) = test

    LOGGER.info("Training baseline and taxonomic models...")
    baseline_result = train_baseline_and_taxonomic_models(
        train_features, train_labels, train_indices, test_features, test_labels, test_indices, taxonomy_categories
    )

    LOGGER.info("Generating taxonomy quantile thresholds...")
    taxonomy_thresholds = generate_taxonomy_quantile_thresholds(memories_dataset)

    LOGGER.info("Starting to train all taxonomy pairs...")
    train_all_taxonomy_pairs(
        train_features,
        train_labels,
        train_indices,
        validation_features,
        validation_labels,
        validation_indices,
        baseline_result.model,
        taxonomy_thresholds,
        pile_dataset,
    )


if __name__ == "__main__":
    main()
