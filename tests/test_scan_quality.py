"""Mesures de qualité, plan de correction et réglages de la numérisation fidèle (pages synthétiques).

Cas : page sombre, ombre, photographie couleur, flou, bruit, inclinaison, page correcte (aucun traitement),
bande de fond au bord, dégradation détectée, actions Gemma non confirmées, actions géométriques.
"""
from __future__ import annotations

import cv2
import numpy as np
import pytest

from models.scanning import ScanAction, ScanAdjustments, VisualAnalysis
from services import image_enhancement, image_quality_service
from tests.fixtures import synthetic
from tests.fixtures.fake_ollama import VALID_ANALYSIS


@pytest.fixture(scope="module")
def clean() -> np.ndarray:
    return synthetic.text_page().image


@pytest.fixture(scope="module")
def photo() -> np.ndarray:
    return synthetic.photo_text_page().image


def _analysis(**changes) -> VisualAnalysis:
    return VisualAnalysis.model_validate({**VALID_ANALYSIS, "recommended_actions": [], "shadow_level": "none",
                                          "lighting_issue": "none", **changes})


def _darken(rgb: np.ndarray, factor: float) -> np.ndarray:
    return np.clip(rgb.astype(np.float32) * factor, 0, 255).astype(np.uint8)


def _shadow(rgb: np.ndarray) -> np.ndarray:
    height, width = rgb.shape[:2]
    ramp = np.linspace(1.0, 0.45, width, dtype=np.float32)[None, :, None]
    return np.clip(rgb.astype(np.float32) * ramp, 0, 255).astype(np.uint8)


def test_clean_page_gets_neutral_plan_and_identity(clean):
    metrics = image_quality_service.measure(clean)
    plan = image_enhancement.plan_adjustments(metrics)
    assert plan.source == "neutral" and plan.adjustments.is_neutral()
    assert not plan.review_required
    out = image_enhancement.apply_adjustments(clean, plan.adjustments)
    assert out is not clean and np.array_equal(out, clean)
    check = image_quality_service.compare(clean, out, metrics)
    assert check.passed and not check.enhancement_degraded and check.color_shift == 0


def test_dark_page_is_brightened_without_color_change(clean):
    dark = _darken(clean, 0.62)
    metrics = image_quality_service.measure(dark)
    assert metrics.paper_level < 185
    plan = image_enhancement.plan_adjustments(metrics)
    assert ScanAction.LIGHTING_CORRECTION in plan.actions and plan.adjustments.exposure > 0
    out = image_enhancement.apply_adjustments(dark, plan.adjustments)
    after = image_quality_service.measure(out)
    assert after.paper_level > metrics.paper_level + 30
    check = image_quality_service.compare(dark, out, metrics)
    assert check.passed and check.color_shift < 3


def test_shadow_is_measured_and_corrected(clean):
    shaded = _shadow(clean)
    metrics = image_quality_service.measure(shaded)
    assert metrics.illumination_unevenness > 0.3
    plan = image_enhancement.plan_adjustments(metrics)
    assert ScanAction.SHADOW_CORRECTION in plan.actions
    out = image_enhancement.apply_adjustments(shaded, plan.adjustments)
    assert image_quality_service.measure(out).illumination_unevenness < metrics.illumination_unevenness / 2
    assert not image_quality_service.compare(shaded, out, metrics).enhancement_degraded


def test_color_photo_keeps_its_colors(photo):
    metrics = image_quality_service.measure(photo)
    assert metrics.colorfulness > 15
    plan = image_enhancement.plan_adjustments(metrics, _analysis(color_content="full_color",
                                                                 recommended_actions=["PRESERVE_COLOR"]))
    assert ScanAction.PRESERVE_COLOR in plan.actions and not plan.adjustments.white_balance
    out = image_enhancement.apply_adjustments(_darken(photo, 0.8), plan.adjustments)
    assert out.ndim == 3 and out.shape[2] == 3                              # jamais de passage en gris
    assert image_quality_service.colorfulness(out) > 0.7 * metrics.colorfulness


def test_strong_blur_is_flagged_not_restored(clean):
    blurred = cv2.GaussianBlur(clean, (0, 0), 6)
    metrics = image_quality_service.measure(blurred)
    plan = image_enhancement.plan_adjustments(metrics, _analysis(blur_level="high", recommended_actions=["SHARPEN"]))
    assert plan.review_required and ScanAction.SHARPEN in plan.rejected_actions
    assert plan.adjustments.sharpness == 0
    assert any("restaurer" in reason for reason in plan.review_reasons)


def test_light_blur_confirmed_by_gemma_gets_moderate_sharpening(clean):
    soft = cv2.GaussianBlur(clean, (0, 0), 1.4)
    metrics = image_quality_service.measure(soft)
    assert 40 <= metrics.sharpness < 150, metrics.sharpness
    plan = image_enhancement.plan_adjustments(metrics, _analysis(blur_level="medium", recommended_actions=["SHARPEN"]))
    assert ScanAction.SHARPEN in plan.actions and 0 < plan.adjustments.sharpness <= 30


def test_noise_is_measured(clean):
    rng = np.random.default_rng(1)
    noisy = np.clip(clean.astype(np.float32) + rng.normal(0, 12, clean.shape), 0, 255).astype(np.uint8)
    metrics = image_quality_service.measure(noisy)
    assert metrics.noise_sigma > image_quality_service.measure(clean).noise_sigma + 3
    plan = image_enhancement.plan_adjustments(metrics)
    assert ScanAction.DENOISE in plan.actions and plan.adjustments.denoise <= 30


def test_tilted_page_residual_skew_is_measured():
    tilted = synthetic.skewed(synthetic.text_page(), 3.0).image
    metrics = image_quality_service.measure(tilted)
    assert metrics.skew_reliable and abs(metrics.residual_skew_deg - 3.0) < 0.6


def test_gemma_action_without_measurement_is_rejected(clean):
    metrics = image_quality_service.measure(clean)
    plan = image_enhancement.plan_adjustments(metrics, _analysis(shadow_level="high",
                                                                 recommended_actions=["SHADOW_CORRECTION", "DENOISE"]))
    assert ScanAction.SHADOW_CORRECTION in plan.rejected_actions and ScanAction.DENOISE in plan.rejected_actions
    assert plan.adjustments.is_neutral()


def test_geometry_actions_only_ask_for_review(clean):
    metrics = image_quality_service.measure(clean)
    plan = image_enhancement.plan_adjustments(metrics, _analysis(orientation_issue="upside_down",
                                                                 recommended_actions=["ROTATE", "PERSPECTIVE_CORRECTION"]))
    assert plan.review_required and plan.adjustments.is_neutral()
    incoherent = image_enhancement.plan_adjustments(metrics, _analysis(recommended_actions=["ROTATE"]))
    assert ScanAction.ROTATE in incoherent.rejected_actions and not incoherent.review_required


def test_background_strip_is_trimmed_per_edge(clean):
    framed = clean.copy()
    framed[:, :12] = 15                                          # bande de fond à gauche seulement
    metrics = image_quality_service.measure(framed)
    assert metrics.dark_edges[0] > 0 and metrics.dark_edges[2] == 0
    plan = image_enhancement.plan_adjustments(metrics)
    assert plan.adjustments.crop_left > 0 and plan.adjustments.crop_right == 0
    out = image_enhancement.apply_adjustments(framed, plan.adjustments)
    assert out[:, :3].mean() > 150 and out.shape[0] == framed.shape[0]


def test_extreme_settings_are_detected_as_degradation(clean):
    harsh = ScanAdjustments(exposure=40, contrast=50, gamma=2.0)
    out = image_enhancement.apply_adjustments(clean, harsh)
    check = image_quality_service.compare(clean, out)
    assert check.enhancement_degraded and not check.passed
    assert check.reasons


def test_adjustments_never_modify_input(clean):
    copy = clean.copy()
    image_enhancement.apply_adjustments(clean, ScanAdjustments(exposure=10, shadow_correction=30, sharpness=20,
                                                               denoise=10, crop_top=0.01))
    assert np.array_equal(copy, clean)
