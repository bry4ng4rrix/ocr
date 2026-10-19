"""Section 2 de image_service : réglages photographiques, ombres, image OCR, découpes, effacement du texte."""
import cv2
import numpy as np
import pytest

from models import Box, ImageEnhancement
from services import image_service
from tests.fixtures import synthetic


@pytest.fixture(scope="module")
def page_rgb() -> np.ndarray:
    return synthetic.text_page().image


@pytest.fixture(scope="module")
def photo_page():
    return synthetic.photo_text_page()


def _shaded(rgb: np.ndarray) -> np.ndarray:
    """Ombre douce de gauche (sombre) à droite (claire), comme une reliure."""
    width = rgb.shape[1]
    ramp = np.linspace(0.55, 1.0, width, dtype=np.float32)[None, :, None]
    return np.clip(rgb.astype(np.float32) * ramp, 0, 255).astype(np.uint8)


def _paper_pixels(rgb: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Valeurs grises des pixels qui sont du papier sur la page de référence (sans encre)."""
    gray_reference = cv2.cvtColor(reference, cv2.COLOR_RGB2GRAY)
    paper = cv2.erode((gray_reference > 200).astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)[paper].astype(np.float32)


def _chroma(rgb: np.ndarray) -> float:
    ycc = cv2.cvtColor(rgb, cv2.COLOR_RGB2YCrCb).astype(np.float32)
    return float(np.mean(np.hypot(ycc[..., 1] - 128, ycc[..., 2] - 128)))


# --------------------------------------------------------------------------- enhance


def test_neutral_settings_are_identity(page_rgb):
    for settings in (ImageEnhancement(), ImageEnhancement(enabled=False, brightness=50, contrast=30)):
        out = image_service.enhance(page_rgb, settings)
        assert np.array_equal(out, page_rgb)
        assert out is not page_rgb and not np.shares_memory(out, page_rgb)


@pytest.mark.parametrize("settings", [
    ImageEnhancement(white_balance="auto"), ImageEnhancement(brightness=40), ImageEnhancement(contrast=-40),
    ImageEnhancement(saturation=50), ImageEnhancement(sharpness=80), ImageEnhancement(denoise=60),
    ImageEnhancement(shadow_correction=100), ImageEnhancement(readability=80),
    ImageEnhancement(brightness=-20, contrast=30, saturation=-50, sharpness=30, denoise=20, readability=30,
                     shadow_correction=50, white_balance="auto"),
])
def test_enhance_keeps_shape_and_never_modifies_input(photo_page, settings):
    source = photo_page.image.copy()
    out = image_service.enhance(source, settings)
    assert np.array_equal(source, photo_page.image), "l'entrée a été modifiée en place"
    assert out.shape == source.shape and out.dtype == np.uint8
    assert len(np.unique(out)) > 2, "jamais de binarisation"


def test_brightness_and_contrast_go_the_right_way(photo_page):
    rgb = photo_page.image
    left, top, right, bottom = photo_page.images[0].box_px
    photo = (slice(top, bottom), slice(left, right))           # tons moyens
    brighter = image_service.enhance(rgb, ImageEnhancement(brightness=40))
    darker = image_service.enhance(rgb, ImageEnhancement(brightness=-40))
    assert brighter[photo].mean() > rgb[photo].mean() + 15
    assert darker[photo].mean() < rgb[photo].mean() - 15
    assert abs(int(brighter[5, 5].mean()) - int(rgb[5, 5].mean())) <= 4, "le papier ne doit pas être brûlé"
    assert image_service.enhance(rgb, ImageEnhancement(contrast=50))[photo].std() > rgb[photo].std() * 1.3
    assert image_service.enhance(rgb, ImageEnhancement(contrast=-50))[photo].std() < rgb[photo].std() * 0.8


def test_saturation_changes_chroma_only(photo_page):
    rgb = photo_page.image
    more = image_service.enhance(rgb, ImageEnhancement(saturation=60))
    less = image_service.enhance(rgb, ImageEnhancement(saturation=-60))
    assert _chroma(more) > _chroma(rgb) * 1.3 and _chroma(less) < _chroma(rgb) * 0.6
    luma = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    assert np.abs(cv2.cvtColor(less, cv2.COLOR_RGB2GRAY) - luma).mean() < 1.5


def test_auto_white_balance_neutralises_a_paper_cast(page_rgb):
    cast = np.clip(page_rgb.astype(np.float32) * np.array([1.0, 0.93, 0.75], np.float32), 0, 255).astype(np.uint8)
    out = image_service.enhance(cast, ImageEnhancement(white_balance="auto"))
    paper_before = cast[50:150, 50:150].reshape(-1, 3).mean(axis=0)
    paper_after = out[50:150, 50:150].reshape(-1, 3).mean(axis=0)
    assert np.ptp(paper_after) < 0.3 * np.ptp(paper_before)


def test_sharpness_increases_edges_and_denoise_reduces_noise(page_rgb):
    blurred = cv2.GaussianBlur(page_rgb, (0, 0), 1.5)
    sharpened = image_service.enhance(blurred, ImageEnhancement(sharpness=80))
    laplacian = lambda image: cv2.Laplacian(cv2.cvtColor(image, cv2.COLOR_RGB2GRAY), cv2.CV_64F).var()  # noqa: E731
    assert laplacian(sharpened) > 1.5 * laplacian(blurred)

    rng = np.random.default_rng(3)
    noisy = np.clip(page_rgb.astype(np.float32) + rng.normal(0, 12, page_rgb.shape), 0, 255).astype(np.uint8)
    denoised = image_service.enhance(noisy, ImageEnhancement(denoise=80))
    patch = (slice(20, 120), slice(20, 300))                       # papier sans texte
    assert denoised[patch].std() < 0.7 * noisy[patch].std()


def test_shadow_correction_flattens_paper(page_rgb):
    shaded = _shaded(page_rgb)
    corrected = image_service.enhance(shaded, ImageEnhancement(shadow_correction=100))
    before, after = _paper_pixels(shaded, page_rgb), _paper_pixels(corrected, page_rgb)
    assert after.std() < 0.35 * before.std()
    half = image_service.correct_shadows(shaded, 0.5)
    assert after.std() < _paper_pixels(half, page_rgb).std() < before.std()
    assert np.array_equal(image_service.correct_shadows(shaded, 0.0), shaded)


def test_shadow_correction_does_not_whiten_illustrations(photo_page):
    shaded = _shaded(photo_page.image)
    corrected = image_service.correct_shadows(shaded, 1.0)
    left, top, right, bottom = photo_page.images[0].box_px
    before = shaded[top:bottom, left:right].astype(np.float32).mean()
    after = corrected[top:bottom, left:right].astype(np.float32).mean()
    assert after < 1.6 * before and after < 200


def test_readability_increases_local_contrast(page_rgb):
    faded = (page_rgb.astype(np.float32) * 0.35 + 140).astype(np.uint8)    # encre grise sur papier gris
    out = image_service.enhance(faded, ImageEnhancement(readability=100))
    gray_before, gray_after = (cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) for image in (faded, out))
    ink = cv2.cvtColor(page_rgb, cv2.COLOR_RGB2GRAY) < 100
    contrast_before = gray_before[~ink].mean() - gray_before[ink].mean()
    contrast_after = gray_after[~ink].mean() - gray_after[ink].mean()
    assert contrast_after > 1.1 * contrast_before          # CLAHE : effet volontairement modéré


def test_readability_keeps_solid_blacks():
    image = np.full((200, 200, 3), 230, np.uint8)
    image[50:150, 50:150] = 10
    out = image_service.enhance(image, ImageEnhancement(readability=100))
    assert out[60:140, 60:140].mean() < 25


# --------------------------------------------------------------------------- image OCR, découpes, fonds


def test_prepare_ocr_image_is_gray_flat_and_blanks_illustrations(photo_page):
    shaded = _shaded(photo_page.image)
    illustration = photo_page.box(photo_page.images[0].box_px)
    gray = image_service.prepare_ocr_image(shaded, [illustration])
    assert gray.ndim == 2 and gray.dtype == np.uint8 and gray.shape == shaded.shape[:2]
    left, top, right, bottom = illustration.to_pixels(gray.shape[1], gray.shape[0])
    assert (gray[top:bottom, left:right] == 255).all()
    paper = _paper_pixels(cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB), photo_page.image)
    assert paper.std() < 6 and 230 < np.median(paper) < 252
    assert len(np.unique(gray)) > 10, "pas de binarisation"


def test_crop_box_is_lossless_full_resolution(photo_page):
    rgb = photo_page.image
    box = photo_page.box(photo_page.images[0].box_px)
    crop = image_service.crop_box(rgb, box)
    left, top, right, bottom = box.to_pixels(rgb.shape[1], rgb.shape[0])
    assert np.array_equal(crop, rgb[top:bottom, left:right])
    crop[:] = 0
    assert rgb[top:bottom, left:right].any(), "la découpe ne partage pas la mémoire de la page"
    padded = image_service.crop_box(rgb, box, padding=0.02)
    assert padded.shape[0] > bottom - top and padded.shape[1] > right - left


@pytest.mark.parametrize("method", ["inpaint", "fill"])
def test_erase_text_regions_removes_ink_only_inside_boxes(graphic_page, method):
    rgb = graphic_page.image
    on_image = [block for block in graphic_page.blocks if block.on_image]
    boxes = [graphic_page.box(block.box_px) for block in on_image]
    out = image_service.erase_text_regions(rgb, boxes, method=method)
    assert out.shape == rgb.shape and not np.array_equal(out, rgb)
    height, width = rgb.shape[:2]
    inside = np.zeros((height, width), bool)
    for box in boxes:
        left, top, right, bottom = box.to_pixels(width, height)
        inside[top:bottom, left:right] = True
        region_before = cv2.cvtColor(rgb[top:bottom, left:right], cv2.COLOR_RGB2GRAY)
        region_after = cv2.cvtColor(out[top:bottom, left:right], cv2.COLOR_RGB2GRAY)
        bright_before = (region_before > 200).mean()
        assert (region_after > 200).mean() < 0.25 * bright_before + 0.002, "le texte clair est resté"
    assert np.array_equal(out[~inside], rgb[~inside])


def test_binarize_sauvola_handles_uneven_light(page_rgb):
    gray = cv2.cvtColor(_shaded(page_rgb), cv2.COLOR_RGB2GRAY)
    binary = image_service.binarize_sauvola(gray)
    assert set(np.unique(binary)) <= {0, 255}
    ink = cv2.cvtColor(page_rgb, cv2.COLOR_RGB2GRAY) < 100
    assert (binary[~ink] == 255).mean() > 0.97
    assert (binary[ink] == 0).mean() > 0.8
