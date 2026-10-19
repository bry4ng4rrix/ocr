"""Correction géométrique : orientation, feuille/perspective, inclinaison, courbure, page complète."""
import time

import cv2
import numpy as np
import pytest

import config
from models import GeometryCorrection, Point
from services import image_service, ocr_service, orientation_service
from tests.conftest import requires_languages
from tests.fixtures import synthetic

needs_osd = requires_languages("fra", "osd")


@pytest.fixture(scope="module")
def page():
    return synthetic.text_page()


def _forbid(monkeypatch, *names):
    def fail(*args, **kwargs):
        raise AssertionError("détection appelée alors qu'elle devait être rejouée")
    for name in names:
        monkeypatch.setattr(orientation_service, name, fail)


# --------------------------------------------------------------------------- orientation


@needs_osd
@pytest.mark.tesseract
@pytest.mark.parametrize("turns", [0, 1, 2, 3])
def test_osd_finds_quarter_turns(page, turns):
    rotated = synthetic.rotated(page, turns)
    result = orientation_service.detect_orientation(rotated.image, ["fra"])
    assert result.quarter_turns == rotated.expected_fix_cw
    assert not result.uncertain and result.method in ("osd", "reading")


@needs_osd
@pytest.mark.tesseract
@pytest.mark.parametrize("turns", [1, 2])
def test_reading_check_when_osd_has_no_answer(page, monkeypatch, turns):
    monkeypatch.setattr(ocr_service, "run_osd", lambda *args, **kwargs: None)
    rotated = synthetic.rotated(page, turns)
    result = orientation_service.detect_orientation(rotated.image, ["fra"])
    assert (result.quarter_turns, result.method, result.uncertain) == (rotated.expected_fix_cw, "reading", False)


@needs_osd
@pytest.mark.tesseract
def test_low_osd_confidence_is_checked_by_reading(page, monkeypatch):
    """Une réponse OSD peu sûre et fausse (180°) est corrigée par la lecture."""
    monkeypatch.setattr(ocr_service, "run_osd",
                        lambda *args, **kwargs: ocr_service.OsdResult(rotate=180, orientation_confidence=3.0))
    result = orientation_service.detect_orientation(page.image, ["fra"])
    assert result.quarter_turns == 0 and result.method == "reading" and not result.uncertain


@needs_osd
@pytest.mark.tesseract
def test_page_without_text_is_uncertain_and_not_turned():
    blank = synthetic.blank_page("vide").image
    result = orientation_service.detect_orientation(blank, ["fra"])
    assert result.quarter_turns == 0 and result.uncertain


def test_orientation_without_tesseract_is_uncertain(page, monkeypatch):
    monkeypatch.setattr(config, "TESSERACT_CMD", "/chemin/absent/tesseract")
    result = orientation_service.detect_orientation(page.image)
    assert (result.quarter_turns, result.method, result.uncertain) == (0, "none", True)


# --------------------------------------------------------------------------- inclinaison


@pytest.mark.parametrize("angle", [-3.0, 3.0, -1.2, 6.5])
def test_skew_is_measured(page, angle):
    skewed = synthetic.skewed(page, angle)
    result = orientation_service.estimate_skew(image_service.to_gray(skewed.image))
    assert abs(result.angle_deg - angle) <= 0.5 and not result.uncertain


def test_straight_page_has_zero_skew(page):
    result = orientation_service.estimate_skew(image_service.to_gray(page.image))
    assert result.angle_deg == 0.0 and not result.uncertain


def test_blank_page_skew_is_uncertain():
    result = orientation_service.estimate_skew(image_service.to_gray(synthetic.blank_page("vide").image))
    assert result.uncertain and result.angle_deg == 0.0


# --------------------------------------------------------------------------- feuille et perspective


@pytest.mark.parametrize("seed", [1, 5, 9])
@pytest.mark.parametrize("factory", [synthetic.text_page, synthetic.photo_text_page, synthetic.graphic_background_page])
def test_sheet_corners_found_within_two_percent(factory, seed):
    photo = synthetic.on_dark_background(factory(), seed=seed)
    found = orientation_service.detect_page_quad(photo.image)
    assert found is not None and not found.uncertain
    for corner, (x, y) in zip(found.corners, photo.quad):
        assert abs(corner.x - x) <= 0.02 and abs(corner.y - y) <= 0.02


@pytest.mark.parametrize("factory", [synthetic.text_page, synthetic.photo_text_page, synthetic.graphic_background_page])
def test_no_sheet_when_page_fills_the_image(factory):
    assert orientation_service.detect_page_quad(factory().image) is None


def test_no_sheet_on_uniform_dark_image():
    assert orientation_service.detect_page_quad(np.full((600, 800, 3), 30, np.uint8)) is None


def test_sheet_cut_off_by_the_frame_is_refused(page):
    photo = synthetic.on_dark_background(page)
    height = photo.image.shape[0]
    cropped = np.ascontiguousarray(photo.image[: int(0.6 * height)])     # bas de la feuille hors cadre
    assert orientation_service.detect_page_quad(cropped) is None


def test_next_page_below_a_thin_gap_is_excluded(page):
    """Comme sur le banc : la page suivante, claire, apparaît sous la feuille, séparée par un pli sombre."""
    photo = synthetic.on_dark_background(page, jitter=0.0)
    canvas = photo.image.copy()
    height, width = canvas.shape[:2]
    bottom = int(round(0.88 * height))
    canvas[bottom + 6:, int(0.1 * width):int(0.9 * width)] = (215, 215, 210)    # page suivante
    canvas[bottom:bottom + 6, :] = (70, 70, 70)                                  # pli
    canvas = cv2.GaussianBlur(canvas, (5, 5), 0)
    found = orientation_service.detect_page_quad(canvas)
    assert found is not None
    assert max(corner.y for corner in found.corners) < 0.89


def test_warp_quad_restores_page_size(page):
    photo = synthetic.on_dark_background(page, jitter=0.0)
    corners = [Point(x=x, y=y) for x, y in photo.quad]
    warped = orientation_service.warp_quad(photo.image, corners)
    canvas_h, canvas_w = photo.image.shape[:2]
    (left, top), (right, _), (_, bottom) = photo.quad[0], photo.quad[1], photo.quad[2]
    assert abs(warped.shape[1] - (right - left) * canvas_w) <= 2
    assert abs(warped.shape[0] - (bottom - top) * canvas_h) <= 2
    expected = cv2.resize(page.image, (warped.shape[1], warped.shape[0]), interpolation=cv2.INTER_AREA)
    assert np.abs(warped.astype(np.int16) - expected).mean() < 8


# --------------------------------------------------------------------------- courbure


def test_curved_lines_are_straightened(page):
    curved = synthetic.curved(page, amplitude_px=25)
    before = orientation_service.estimate_curvature(image_service.to_gray(curved.image))
    assert before.applicable and before.lines_used >= 8 and 15 <= before.max_offset_px <= 35
    out, result = orientation_service.dewarp(curved.image)
    assert result.applicable and out.shape == curved.image.shape
    after = orientation_service.estimate_curvature(image_service.to_gray(out))
    assert after.max_offset_px < 0.25 * before.max_offset_px
    again, _ = orientation_service.dewarp(curved.image)
    assert np.array_equal(out, again), "la correction de courbure doit être déterministe"


@pytest.mark.parametrize("factory", [synthetic.text_page, synthetic.two_column_page, synthetic.photo_text_page,
                                     synthetic.graphic_background_page])
def test_flat_or_illustrated_pages_are_not_dewarped(factory):
    rgb = factory().image
    out, result = orientation_service.dewarp(rgb)
    assert not result.applicable and result.reason
    assert np.array_equal(out, rgb)


def test_curvature_is_never_applied_with_a_large_illustration():
    curved_photo = synthetic.curved(synthetic.photo_text_page(), amplitude_px=25)
    out, result = orientation_service.dewarp(curved_photo.image)
    assert not result.applicable and "illustration" in result.reason
    assert np.array_equal(out, curved_photo.image)


# --------------------------------------------------------------------------- page complète


@needs_osd
@pytest.mark.tesseract
def test_straight_sharp_page_comes_out_almost_unchanged(page):
    source = page.image.copy()
    outcome = orientation_service.correct_page(source, GeometryCorrection(), languages=["fra"])
    assert np.array_equal(source, page.image), "l'original a été modifié"
    assert outcome.image.shape == page.image.shape
    assert np.abs(outcome.image.astype(np.int16) - page.image).mean() < 2
    geometry = outcome.geometry
    assert geometry.detection_done and geometry.detected_quad is None
    assert geometry.detected_quarter_turns == 0 and not geometry.orientation_uncertain
    assert geometry.detected_skew_deg == 0.0 and not geometry.dewarp_applied
    assert not any(issue.severity == "warning" for issue in outcome.issues)
    assert not np.shares_memory(outcome.image, source)


@needs_osd
@pytest.mark.tesseract
def test_photographed_rotated_skewed_page_is_restored(page):
    """Feuille inclinée de 2°, tournée d'un quart de tour, photographiée en perspective sur fond sombre."""
    photo = synthetic.on_dark_background(synthetic.rotated(synthetic.skewed(page, 2.0), 1), seed=3)
    settings = GeometryCorrection()
    start = time.perf_counter()
    outcome = orientation_service.correct_page(photo.image, settings, languages=["fra"])
    elapsed = time.perf_counter() - start
    geometry = outcome.geometry
    assert settings.detected_quad is None, "les réglages d'entrée ne doivent pas être modifiés"
    assert geometry.detected_quad is not None and not geometry.quad_uncertain
    assert geometry.detected_quarter_turns == 3
    assert abs(geometry.detected_skew_deg - 2.0) <= 0.5
    height, width = outcome.image.shape[:2]
    assert height > width and abs(width / height - page.width / page.height) < 0.03
    gray = image_service.to_gray(outcome.image)
    assert orientation_service.estimate_skew(gray).angle_deg == pytest.approx(0.0, abs=0.3)
    assert elapsed < 30


@needs_osd
@pytest.mark.tesseract
def test_replay_without_detection_is_reproducible(page, monkeypatch):
    photo = synthetic.on_dark_background(synthetic.skewed(synthetic.curved(page, 20), 1.5))
    first = orientation_service.correct_page(photo.image, GeometryCorrection(), languages=["fra"])
    assert first.geometry.dewarp_applied
    _forbid(monkeypatch, "detect_page_quad", "detect_orientation", "estimate_skew")
    second = orientation_service.correct_page(photo.image, first.geometry, detect=False)
    assert np.array_equal(first.image, second.image)
    assert second.geometry == first.geometry


def test_replay_applies_manual_values_first(page, monkeypatch):
    _forbid(monkeypatch, "detect_page_quad", "detect_orientation", "estimate_skew")
    photo = synthetic.on_dark_background(page, jitter=0.0)
    manual = [Point(x=x, y=y) for x, y in photo.quad]
    settings = GeometryCorrection(detected_quarter_turns=2, detected_skew_deg=4.0, manual_quarter_turns=3,
                                  manual_skew_deg=0.0, manual_quad=manual, detection_done=True, auto_dewarp=False)
    outcome = orientation_service.correct_page(photo.image, settings, detect=False)
    # quart de tour effectif = 2 (auto) + 3 (manuel) = 1 ; angle manuel 0 prioritaire ; contour manuel
    expected = orientation_service.rotate_quarter(orientation_service.warp_quad(photo.image, manual), 1)
    assert np.array_equal(outcome.image, expected)


def test_detection_with_manual_quad_does_not_search_the_sheet(page, monkeypatch):
    _forbid(monkeypatch, "detect_page_quad")
    monkeypatch.setattr(orientation_service, "detect_orientation",
                        lambda *args, **kwargs: orientation_service.OrientationResult(
                            quarter_turns=0, confidence=0.0, method="none", uncertain=True))
    photo = synthetic.on_dark_background(page)
    settings = GeometryCorrection(manual_quad=[Point(x=x, y=y) for x, y in photo.quad])
    outcome = orientation_service.correct_page(photo.image, settings)
    assert outcome.geometry.orientation_uncertain
    assert [issue.code for issue in outcome.issues] == ["orientation_uncertain"]
    assert outcome.image.shape[0] > outcome.image.shape[1]


def test_uncertain_detections_are_reported_not_applied(monkeypatch):
    monkeypatch.setattr(orientation_service, "detect_orientation",
                        lambda *args, **kwargs: orientation_service.OrientationResult(
                            quarter_turns=0, confidence=0.0, method="none", uncertain=True))
    blank = synthetic.blank_page("vide").image
    outcome = orientation_service.correct_page(blank, GeometryCorrection())
    codes = {issue.code for issue in outcome.issues}
    assert {"orientation_uncertain", "skew_uncertain", "quad_not_found"} <= codes
    assert np.array_equal(outcome.image, blank)
    for issue in outcome.issues:
        assert issue.message and issue.message[0].isupper()


def test_dewarp_disabled_is_respected(page, monkeypatch):
    monkeypatch.setattr(orientation_service, "detect_orientation",
                        lambda *args, **kwargs: orientation_service.OrientationResult(
                            quarter_turns=0, confidence=20.0, method="osd", uncertain=False))
    curved = synthetic.curved(page, 25).image
    outcome = orientation_service.correct_page(curved, GeometryCorrection(auto_dewarp=False))
    assert not outcome.geometry.dewarp_applied and np.array_equal(outcome.image, curved)
    enabled = orientation_service.correct_page(curved, GeometryCorrection())
    assert enabled.geometry.dewarp_applied and "dewarp_applied" in {issue.code for issue in enabled.issues}


# --------------------------------------------------------------------------- photos réelles (banc de numérisation)

# Orientation vérifiée à l'œil sur les copies (quarts de tour horaires qui redressent le texte).
REAL_EXPECTED_TURNS = {"00011": 1, "00013": 1, "00020": 3, "00021": 1, "00022": 3, "00023": 1, "00043": 2}


@pytest.mark.slow
@pytest.mark.real_images
@pytest.mark.tesseract
@needs_osd
def test_real_bench_photos_are_cropped_and_straightened(real_pages, tmp_path):
    """Copies des photos réelles (la fixture vérifie que les originaux ne changent pas)."""
    from services import ocr_service as ocr

    languages = [lang for lang in ("fra", "eng") if lang in ocr.available_languages()]
    previews = tmp_path / "apercus"
    outcomes = {}
    for path in real_pages:
        rgb = image_service.read_image(path)
        if rgb.shape[:2] != (3712, 5568):          # seulement les photos du banc
            continue
        outcome = orientation_service.correct_page(rgb, GeometryCorrection(), languages=languages)
        image_service.write_jpeg(image_service.thumbnail(outcome.image, 800), previews / f"{path.stem}.jpg")
        outcomes[path.stem] = outcome
    if not outcomes:
        pytest.skip("aucune photo du banc dans le dossier des photos réelles")
    with_sheet = [name for name, outcome in outcomes.items() if outcome.geometry.detected_quad]
    assert len(with_sheet) >= 0.8 * len(outcomes), f"feuilles trouvées : {with_sheet}"
    for name, outcome in outcomes.items():
        height, width = outcome.image.shape[:2]
        if outcome.geometry.detected_quad:
            assert 0.15 * 3712 * 5568 < height * width < 0.4 * 3712 * 5568, name   # feuille seule, pas le cadre
        if name in REAL_EXPECTED_TURNS:
            assert outcome.geometry.detected_quarter_turns == REAL_EXPECTED_TURNS[name], name
            assert not outcome.geometry.orientation_uncertain, name
    for name in ("00011", "00021"):
        if name in outcomes:
            result = ocr.run_ocr(image_service.prepare_ocr_image(outcomes[name].image), languages)
            assert len(result.words) > 150 and result.mean_confidence > 75, name
