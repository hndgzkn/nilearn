"""
Voxel-Based Morphometry on Oasis dataset
========================================

This example uses Voxel-Based Morphometry (:term:`VBM`)
to study the relationship between aging and gray matter density.

The data come from the `OASIS <https://sites.wustl.edu/oasisbrains/>`_ project.
If you use it, you need to agree with the data usage agreement available
on the website.

It has been run through a standard :term:`VBM` pipeline (using SPM8 and
NewSegment) to create :term:`VBM` maps, which we study here.

Predictive modeling analysis: VBM bio-markers of aging?
-------------------------------------------------------

We run a standard SVM-ANOVA nilearn pipeline to predict age from the VBM
data. We use only 100 subjects from the OASIS dataset to limit the memory
usage.

Note that for an actual predictive modeling study of aging, the study
should be ran on the full set of subjects. Also, all parameters should be set
by cross-validation. This includes the smoothing applied to the data and the
number of features selected by the :term:`ANOVA` step. Indeed, even these
data-preparation parameter impact significantly the prediction score.


Also, parameters such as the smoothing should be applied to the data and the
number of features selected by the :term:`ANOVA` step should be set by nested
cross-validation, as they impact significantly the prediction score.

Brain mapping with mass univariate
----------------------------------

:term:`SVM` weights are very noisy,
partly because heavy smoothing is detrimental for the prediction here.
A standard analysis using mass-univariate :term:`GLM`
(here permuted to have exact correction for multiple comparisons) gives a
much clearer view of the important regions.

.. seealso::

    For more information
    see the :ref:`dataset description <oasis_maps>`.

____

.. include:: ../../../examples/masker_note.rst
"""

# %%
import numpy as np

from nilearn.datasets import fetch_oasis_vbm
from nilearn.image import get_data
from nilearn.maskers import NiftiMasker

n_subjects = 200  # more subjects requires more memory

# %%
# Load Oasis dataset
# ------------------
oasis_dataset = fetch_oasis_vbm(n_subjects=n_subjects)
gray_matter_map_filenames = oasis_dataset.gray_matter_maps
age = oasis_dataset.ext_vars["age"].to_numpy()

# Split data into training set and test set
from sklearn.model_selection import train_test_split

gm_imgs_train, gm_imgs_test, age_train, age_test = train_test_split(
    gray_matter_map_filenames, age, train_size=0.6, random_state=0
)

# print basic information on the dataset
print(
    "First gray-matter anatomy image (3D) is located at: "
    f"{oasis_dataset.gray_matter_maps[0]}"
)
print(
    "First white-matter anatomy image (3D) is located at: "
    f"{oasis_dataset.white_matter_maps[0]}"
)

# %%
# Preprocess data
# ---------------
nifti_masker = NiftiMasker(
    standardize=False, smoothing_fwhm=2, memory="nilearn_cache"
)  # cache options
gm_maps_masked = nifti_masker.fit_transform(gm_imgs_train)

# The features with too low between-subject variance are removed using
# :class:`sklearn.feature_selection.VarianceThreshold`.
from sklearn.feature_selection import VarianceThreshold

variance_threshold = VarianceThreshold(threshold=0.01)
variance_threshold.fit_transform(gm_maps_masked)

# Then we convert the data back to the mask image in order to use it for
# decoding process
mask = nifti_masker.inverse_transform(variance_threshold.get_support())

# %%
# Prediction pipeline with :term:`ANOVA` and SVR using
# :class:`~nilearn.decoding.DecoderRegressor` Object
#
# In nilearn we can benefit from the built-in DecoderRegressor object to
# do :term:`ANOVA` with SVR instead of manually defining the whole pipeline.
# This estimator also uses Cross Validation to select best models and ensemble
# them. Furthermore, you can pass ``n_jobs=<some_high_value>`` to the
# DecoderRegressor class to take advantage of a multi-core system.
# To save time (because these are anat images with many voxels), we include
# only the 1-percent voxels most correlated with the age variable to fit. We
# also want to set mask hyperparameter to be the mask we just obtained above.

from nilearn.decoding import DecoderRegressor

decoder = DecoderRegressor(
    estimator="svr",
    mask=mask,
    scoring="neg_mean_absolute_error",
    screening_percentile=1,
    n_jobs=2,
    standardize="zscore_sample",
)
# Fit and predict with the decoder
decoder.fit(gm_imgs_train, age_train)

# Sort test data for better visualization (trend, etc.)
perm = np.argsort(age_test)[::-1]
age_test = age_test[perm]
gm_imgs_test = np.array(gm_imgs_test)[perm]
age_pred = decoder.predict(gm_imgs_test)

prediction_score = -np.mean(decoder.cv_scores_["beta"])

print("=== DECODER ===")
print(f"explained variance for the cross-validation: {prediction_score:f}")
print()

# %%
# Visualization
# -------------
weight_img = decoder.coef_img_["beta"]

# Create the figure
from nilearn.plotting import plot_stat_map, show

bg_filename = gray_matter_map_filenames[0]
z_slice = 0
display = plot_stat_map(
    weight_img,
    bg_img=bg_filename,
    display_mode="z",
    cut_coords=[z_slice],
    title="SVM weights",
    cmap="cold_hot",
)
show()

# %%
# Visualize the quality of predictions
# ------------------------------------
import matplotlib.pyplot as plt

plt.figure(figsize=(6, 4.5))
plt.suptitle(f"Decoder: Mean Absolute Error {prediction_score:.2f} years")
linewidth = 3
plt.plot(age_test, label="True age", linewidth=linewidth)
plt.plot(age_pred, "--", c="g", label="Predicted age", linewidth=linewidth)
plt.ylabel("age")
plt.xlabel("subject")
plt.legend(loc="best")
plt.figure(figsize=(6, 4.5))
plt.plot(
    age_test - age_pred, label="True age - predicted age", linewidth=linewidth
)
plt.xlabel("subject")
plt.legend(loc="best")

# %%
# Inference with massively univariate model
# -----------------------------------------
print("Massively univariate model")

gm_maps_masked = NiftiMasker().fit_transform(gray_matter_map_filenames)
data = variance_threshold.fit_transform(gm_maps_masked)

# Statistical inference
from nilearn.mass_univariate import permuted_ols

# This can be changed to use more CPUs.
output = permuted_ols(
    age,
    data,  # + intercept as a covariate by default
    n_perm=2000,  # 1,000 in the interest of time; 10000 would be better
    verbose=1,  # display progress bar
    n_jobs=2,
    output_type="dict",
)
neg_log_pvals = output["logp_max_t"]
t_scores_original_data = output["t"]
signed_neg_log_pvals = neg_log_pvals * np.sign(t_scores_original_data)
signed_neg_log_pvals_unmasked = nifti_masker.inverse_transform(
    variance_threshold.inverse_transform(signed_neg_log_pvals)
)

# Show results
threshold = -np.log10(0.1)  # 10% corrected

n_detections = (get_data(signed_neg_log_pvals_unmasked) > threshold).sum()

title = (
    "Negative $\\log_{10}$ p-values\n(Non-parametric + max-type correction)"
    f"\n{int(n_detections)} detections"
)

plot_stat_map(
    signed_neg_log_pvals_unmasked,
    bg_img=bg_filename,
    threshold=threshold,
    display_mode="z",
    cut_coords=[z_slice],
    figure=plt.figure(figsize=(5.5, 7.5), facecolor="k"),
    title=title,
)

show()

# sphinx_gallery_dummy_images=2
