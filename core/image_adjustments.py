"""Photometric corrections of a page or an illustration (pure functions on RGB uint8 arrays).

`apply_adjustments` follows the order documented on `ImageAdjustments`, always starting from the
given reference image (nothing is ever cumulated). Work is done in float32 0..1:

- the per-channel point operations at the start (white balance, exposure, temperature/tint and,
  when no cross-channel step comes before them, brightness/contrast/gamma) are folded into one
  256-entry table applied with `cv2.LUT`: one allocation, exact, and fast on 12 Mpx pages;
- the following steps work in place on that float32 buffer, so a page never needs more than a few
  full-size float32 arrays at once (never float64).

A neutral set (or `enabled=False`) returns the input array itself, untouched.
"""
from __future__ import annotations

import math
from typing import Iterable

import cv2
import numpy as np

from .coordinates import norm_to_px
from .document_models import (AdjustmentTarget, Binarization, ImageAdjustments, LocalAdjustment,
                              WhiteBalance)

LUMA = np.array([0.299, 0.587, 0.114], np.float32)          # Rec. 601 luma (gamma-encoded RGB)
LUMA_LINEAR = np.array([0.2126, 0.7152, 0.0722], np.float64)  # Rec. 709 luminance (linear light)
EXPOSURE_KNEE = 0.75          # linear level above which a positive exposure rolls off softly
MAX_ILLUMINATION_GAIN = 2.2   # never brighten a region more than this when flattening the light
WORK_SIDE = 1000              # analysis copies (picture mask, statistics) are this size at most


# --------------------------------------------------------------------------- small helpers

def _odd(value: float, minimum: int = 3) -> int:
    value = max(minimum, int(round(value)))
    return value if value % 2 else value + 1


def _check_rgb(rgb: np.ndarray) -> None:
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("image RGB uint8 (H, W, 3) attendue")


def _small(img: np.ndarray, max_side: int = WORK_SIDE) -> np.ndarray:
    """Reduced copy for statistics (INTER_AREA), or the image itself when already small."""
    h, w = img.shape[:2]
    scale = max_side / max(h, w)
    if scale >= 1:
        return img
    return cv2.resize(img, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    """sRGB 0..1 -> linear light 0..1 (IEC 61966-2-1)."""
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, None)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def _to_uint8(img: np.ndarray) -> np.ndarray:
    """float 0..1 -> uint8, rounding; modifies `img` in place (it is a working buffer)."""
    np.multiply(img, 255.0, out=img)
    np.add(img, 0.5, out=img)
    np.clip(img, 0.0, 255.0, out=img)
    return img.astype(np.uint8)


def _luma(img: np.ndarray) -> np.ndarray:
    """Luma (H, W) float32 of a float32 RGB image."""
    return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)


# --------------------------------------------------------------------------- statistics

def estimate_white_balance_gains(rgb: np.ndarray, mode: WhiteBalance) -> tuple[float, float, float]:
    """Linear-light channel gains (r, g, b) that neutralise a colour cast.

    GRAY_WORLD: the mean colour becomes grey (luminance kept). WHITE_PATCH: the mean of the
    brightest 3 % of pixels (the paper) becomes white, the brightest channel keeping gain 1.
    Measured on a reduced copy; near-black pixels are ignored; gains are limited to 0.25..4.
    """
    mode = WhiteBalance(mode)
    if mode == WhiteBalance.NONE:
        return (1.0, 1.0, 1.0)
    _check_rgb(rgb)
    small = _small(rgb, 800).reshape(-1, 3)
    lin = srgb_to_linear(small.astype(np.float32) / 255.0)
    valid = small.max(axis=1) > 10
    if valid.sum() < 50:
        valid = np.ones(len(small), bool)
    lin = lin[valid]
    if mode == WhiteBalance.GRAY_WORLD:
        means = lin.mean(axis=0)
        target = float(means @ LUMA_LINEAR)
    else:
        y = lin @ LUMA_LINEAR
        bright = lin[y >= np.percentile(y, 97)]
        means = bright.mean(axis=0)
        target = float(means.max())
    gains = [min(max(target / max(float(m), 1e-4), 0.25), 4.0) for m in means]
    return (gains[0], gains[1], gains[2])


def histogram(rgb: np.ndarray) -> dict[str, np.ndarray]:
    """256-bin counts (int64) of the R, G, B channels and of the luma L (for the interface)."""
    if rgb.ndim == 2:
        h = cv2.calcHist([rgb], [0], None, [256], [0, 256]).ravel().astype(np.int64)
        return {"R": h, "G": h.copy(), "B": h.copy(), "L": h.copy()}
    out = {}
    for i, name in enumerate("RGB"):
        out[name] = cv2.calcHist([rgb], [i], None, [256], [0, 256]).ravel().astype(np.int64)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    out["L"] = cv2.calcHist([gray], [0], None, [256], [0, 256]).ravel().astype(np.int64)
    return out


def _paper_surface(L: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(L, paper lightness surface, paper pixels) for a reduced lightness image.

    The letters are removed by a closing, then a quadratic surface is fitted to the upper
    envelope of the page (iteratively, pixels clearly below the fit are dropped): uneven light
    is modelled, darker pictures are not.
    """
    h, w = L.shape
    closed = cv2.morphologyEx(L, cv2.MORPH_CLOSE, np.ones((_odd(max(h, w) / 50),) * 2, np.uint8))
    step = max(1, int(math.sqrt(h * w / 8000)))
    gy, gx = np.mgrid[0:h:step, 0:w:step]
    v = closed[::step, ::step].ravel().astype(np.float64)
    x = (gx.ravel() / max(w - 1, 1)) * 2 - 1
    y = (gy.ravel() / max(h - 1, 1)) * 2 - 1
    design = np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=1)
    top = max(float(np.percentile(v, 95)), 1.0)
    keep = v >= 0.5 * top
    coef = None
    for _ in range(6):
        if keep.sum() < 30:
            break
        coef = np.linalg.lstsq(design[keep], v[keep], rcond=None)[0]
        fit = design @ coef
        new_keep = v >= 0.92 * fit
        if np.array_equal(new_keep, keep):
            break
        keep = new_keep
    if coef is None:
        surface = np.full((h, w), top, np.float32)
    else:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        xx = xx / max(w - 1, 1) * 2 - 1
        yy = yy / max(h - 1, 1) * 2 - 1
        surface = (coef[0] + coef[1] * xx + coef[2] * yy + coef[3] * xx * xx + coef[4] * xx * yy
                   + coef[5] * yy * yy).astype(np.float32)
    np.clip(surface, max(0.3 * top, 1.0), None, out=surface)
    paper = closed >= 0.92 * surface
    if paper.sum() < 10:
        paper = np.ones_like(paper)
    return L, surface, paper


def _picture_mask_small(small_rgb: np.ndarray) -> np.ndarray:
    """Photos and illustrations (bool) on an already reduced uint8 RGB copy.

    Letters and thin lines are removed by a large opening of the non-paper pixels; what remains
    is a large area. It counts as a picture when it has many tones, real colour, or is a large
    dark area (an underexposed plate); a flat light box does not.
    """
    lab = cv2.cvtColor(small_rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    L, surface, keep = _paper_surface(lab[..., 0])
    # Lightness relative to the paper under the same light (0..100), and colour relative to the
    # paper's own tint: shading and a yellowed sheet are not taken for a picture.
    rel = np.clip(L / surface * 100.0, 0, 100)
    a0, b0 = (float(np.median(lab[..., c][keep])) for c in (1, 2))
    chroma = np.hypot(lab[..., 1] - a0, lab[..., 2] - b0)
    non_paper = ((rel < 88) | (chroma > 18)).astype(np.uint8)
    h, w = L.shape
    size = _odd(max(15, min(h, w) // 40))
    bridged = cv2.morphologyEx(non_paper, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    big = cv2.morphologyEx(bridged, cv2.MORPH_OPEN, np.ones((size, size), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(big, connectivity=8)
    mask = np.zeros((h, w), np.uint8)
    smooth = cv2.medianBlur(np.clip(rel * 2.55, 0, 255).astype(np.uint8), 9)
    for i in range(1, count):
        area = stats[i][4]
        if area < 0.01 * h * w:
            continue
        region = labels == i
        tones = smooth[region]
        colourful = float(np.median(chroma[region])) > 15
        varied = np.percentile(tones, 90) - np.percentile(tones, 10) >= 55
        dark = float(np.median(rel[region])) < 50 and area >= 0.02 * h * w
        if colourful or varied or dark:
            mask[region] = 1
    if mask.any():
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
    return mask.astype(bool)


def picture_mask(rgb: np.ndarray) -> np.ndarray:
    """Pixels (bool, full size) that belong to photos and illustrations."""
    _check_rgb(rgb)
    small = _small(rgb)
    mask = _picture_mask_small(small)
    if small is rgb:
        return mask
    return cv2.resize(mask.astype(np.uint8), (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_NEAREST).astype(bool)


# --------------------------------------------------------------------------- point operations
# These work on any float array whose last axis is (R, G, B): the 256-entry table or a full image.

def _channel_gains(adj: ImageAdjustments) -> np.ndarray:
    """Temperature and tint as linear-light channel gains, normalised to keep the luminance."""
    t, m = adj.temperature / 100.0, adj.tint / 100.0
    gains = np.array([2 ** (0.5 * t), 2 ** (-0.4 * m), 2 ** (-0.5 * t)], np.float64)
    return gains / float(gains @ LUMA_LINEAR)


def _linear_stage(x: np.ndarray, wb: np.ndarray | None, exposure: float, tt: np.ndarray | None) -> np.ndarray:
    """White balance, exposure and temperature/tint in linear light (x: sRGB 0..1)."""
    lin = srgb_to_linear(x)
    if wb is not None:
        lin = lin * wb
    if exposure:
        gain = 2.0 ** exposure
        lin = lin * gain
        if gain > 1:
            # Soft shoulder: above the knee, [knee, gain] is mapped onto [knee, 1] with a slope of 1
            # at the knee, so the old white stays exactly white and bright details are not clipped.
            k = EXPOSURE_KNEE
            a = (gain - k) / (1 - k)
            t = np.clip((lin - k) / (gain - k), 0.0, 1.0)
            lin = np.where(lin > k, k + (1 - k) * (1 - (1 - t) ** a), lin)
    if tt is not None:
        lin = lin * tt
    return linear_to_srgb(np.clip(lin, 0.0, 1.0))


def _brightness_contrast_gamma(x: np.ndarray, adj: ImageAdjustments) -> np.ndarray:
    """Same curve on every channel, in place when `x` is a float32 working buffer."""
    if adj.brightness:
        x += adj.brightness / 250.0
        np.clip(x, 0.0, 1.0, out=x)
    if adj.contrast:
        k = 2.0 ** (adj.contrast / 50.0)       # x4 at +100, x0.25 at -100
        x -= 0.5
        x *= k
        x += 0.5
        np.clip(x, 0.0, 1.0, out=x)
    if adj.gamma != 1.0:
        np.clip(x, 0.0, 1.0, out=x)
        np.power(x, 1.0 / adj.gamma, out=x)
    return x


def _shadows_highlights(img: np.ndarray, adj: ImageAdjustments) -> None:
    """Tone curve on the luma, applied as a ratio to R, G, B (hue and saturation ratios kept).

    Shadows: Y + s·k·Y·(1-Y)² (black and white stay fixed); highlights: the mirrored curve.
    The ratio Y'/Y is bounded, so the darkest pixels never explode.
    """
    y = _luma(img)
    np.clip(y, 0.0, 1.0, out=y)
    # ratio = Y'/Y = 1 + s·k·(1-Y)² + h·k·Y·(1-Y), computed without full-size temporaries
    s, h = adj.shadows / 100.0, adj.highlights / 100.0
    inv = np.subtract(1.0, y, dtype=np.float32)        # 1 - Y
    ratio = np.ones_like(y)
    if s:
        term = np.multiply(inv, inv)
        term *= s * (1.5 if s > 0 else 0.9)
        ratio += term
        del term
    if h:
        term = np.multiply(inv, y)
        term *= h * (1.5 if h > 0 else 0.9)
        ratio += term
        del term
    del inv, y
    img *= ratio[..., None]
    np.clip(img, 0.0, 1.0, out=img)


def _saturation_hue(img: np.ndarray, adj: ImageAdjustments) -> np.ndarray:
    """Scale and turn the chroma in YCbCr (BT.601): one 3x3 matrix, the luma is kept exactly."""
    to_ycc = np.array([[0.299, 0.587, 0.114],
                       [-0.168736, -0.331264, 0.5],
                       [0.5, -0.418688, -0.081312]], np.float64)
    s = 1.0 + adj.saturation / 100.0
    a = math.radians(adj.hue)
    chroma = s * np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    middle = np.eye(3)
    middle[1:, 1:] = chroma
    matrix = (np.linalg.inv(to_ycc) @ middle @ to_ycc).astype(np.float32)
    out = cv2.transform(img, matrix, dst=img)
    np.clip(out, 0.0, 1.0, out=out)
    return out


# --------------------------------------------------------------------------- spatial operations

def illumination_gain(img: np.ndarray) -> np.ndarray:
    """Gain map (H, W) float32 that flattens the paper to white.

    The background is measured on a reduced copy: a closing removes the letters, then only
    paper-like pixels (bright, outside pictures) are kept and the gaps are filled by normalized
    convolution at growing scales. Inside a large illustration (light or dark) the background is
    therefore interpolated from the paper around it: the picture gets the same mild gain as its
    surroundings and is never whitened. Gains are limited to MAX_ILLUMINATION_GAIN.
    """
    h, w = img.shape[:2]
    small = _small(img, 600)
    sh, sw = small.shape[:2]
    y = _luma(small)
    side = max(sh, sw)
    closed = cv2.morphologyEx(y, cv2.MORPH_CLOSE, np.ones((_odd(side / 50),) * 2, np.uint8))
    paper = max(float(np.percentile(closed, 90)), 1e-3)
    pictures = _picture_mask_small(np.clip(small * 255.0 + 0.5, 0, 255).astype(np.uint8))
    mask = ((closed >= 0.5 * paper) & ~pictures).astype(np.float32)
    values = closed * mask
    background = np.full((sh, sw), paper, np.float32)
    filled = np.zeros((sh, sw), bool)
    sigma = side / 25.0
    for _ in range(5):
        num = cv2.GaussianBlur(values, (0, 0), sigma)
        den = cv2.GaussianBlur(mask, (0, 0), sigma)
        ok = (den > 0.05) & ~filled
        background[ok] = num[ok] / den[ok]
        filled |= ok
        if filled.all():
            break
        sigma *= 2.0
    background = cv2.GaussianBlur(background, (0, 0), side / 100.0)
    gain = 1.0 / np.maximum(background, 1.0 / MAX_ILLUMINATION_GAIN)
    if (sh, sw) != (h, w):
        gain = cv2.resize(gain, (w, h), interpolation=cv2.INTER_LINEAR)
    return gain.astype(np.float32, copy=False)


def _clahe(img: np.ndarray, clip: float) -> np.ndarray:
    """CLAHE on L of Lab. Solid blacks keep their lightness (CLAHE would lift them to grey).

    CLAHE needs 8-bit data, so only the *change* it makes is quantized; L itself stays float.
    """
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2Lab)          # L: 0..100
    L = lab[..., 0]
    l8 = np.clip(L * 2.55 + 0.5, 0, 255).astype(np.uint8)
    tiles = (8, 8)
    eq = cv2.createCLAHE(clipLimit=float(clip), tileGridSize=tiles).apply(l8)
    delta = eq.astype(np.float32)
    delta -= l8
    delta /= 2.55
    paper = max(float(np.percentile(_small(l8, 600), 90)) / 2.55, 1.0)
    # Weight of the change: 0 on solid black (below 25 % of the paper), 1 above 45 %.
    weight = np.multiply(L, 1.0 / (0.2 * paper), dtype=np.float32)
    weight -= 0.25 / 0.2
    np.clip(weight, 0.0, 1.0, out=weight)
    delta *= weight
    del weight
    L += delta
    del delta
    np.clip(L, 0.0, 100.0, out=L)
    return cv2.cvtColor(lab, cv2.COLOR_Lab2RGB, dst=img)


def _sharpen(img: np.ndarray, amount: float, radius: float) -> None:
    """Unsharp mask in place: img + amount · (img - blur(img))."""
    blur = cv2.GaussianBlur(img, (0, 0), radius)
    blur -= img
    blur *= amount
    img -= blur
    np.clip(img, 0.0, 1.0, out=img)


def binarize_otsu(gray: np.ndarray) -> np.ndarray:
    """Global Otsu threshold: 0 (ink) / 255 (paper)."""
    return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]


def binarize_sauvola(gray: np.ndarray, window: int | None = None, k: float = 0.2, r: float = 128.0) -> np.ndarray:
    """Sauvola local threshold (uint8 0/255), robust to uneven backgrounds.

    Inside a solid black area the local mean is as dark as the noise, so Sauvola alone would
    speckle it with white: anything clearly darker than the paper is kept black.
    """
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_RGB2GRAY)
    g = cv2.GaussianBlur(gray, (3, 3), 0).astype(np.float32)
    window = _odd(window or max(g.shape) // 40, 25)
    mean = cv2.boxFilter(g, -1, (window, window), borderType=cv2.BORDER_REPLICATE)
    sq = cv2.boxFilter(g * g, -1, (window, window), borderType=cv2.BORDER_REPLICATE)
    sq -= mean * mean
    np.maximum(sq, 0, out=sq)
    np.sqrt(sq, out=sq)                                     # local standard deviation
    threshold = mean * (1 + k * (sq / r - 1))
    paper = float(np.percentile(_small(g, 600), 90))
    return np.where((g > threshold) & (g > 0.4 * paper), 255, 0).astype(np.uint8)


# --------------------------------------------------------------------------- main entry points

def _active(adj: ImageAdjustments) -> bool:
    return not adj.is_neutral() and any((
        adj.white_balance != WhiteBalance.NONE, adj.exposure, adj.temperature, adj.tint,
        adj.shadows, adj.highlights, adj.brightness, adj.contrast, adj.gamma != 1.0,
        adj.saturation, adj.hue, adj.illumination_correction, adj.local_contrast, adj.denoise,
        adj.sharpness, adj.grayscale, adj.binarization != Binarization.NONE))


def apply_adjustments(rgb: np.ndarray, adj: ImageAdjustments) -> np.ndarray:
    """Apply `adj` to an RGB uint8 image; returns a new array (or `rgb` itself when neutral).

    Order (see ImageAdjustments): white balance, exposure, temperature/tint, shadows/highlights,
    brightness, contrast, gamma, saturation/hue, illumination flattening, CLAHE, denoise,
    sharpen, grayscale, binarization. Each step runs only when it differs from its neutral value.
    Grayscale and binarization still return 3 identical channels. `rgb` is never modified.
    """
    _check_rgb(rgb)
    if not _active(adj):
        return rgb

    # 1. Per-channel point operations, folded into one table indexed by the uint8 input.
    wb = None
    if adj.white_balance != WhiteBalance.NONE:
        wb = np.array(estimate_white_balance_gains(rgb, adj.white_balance), np.float64)
    tt = _channel_gains(adj) if (adj.temperature or adj.tint) else None
    table = np.repeat((np.arange(256, dtype=np.float64) / 255.0)[None, :, None], 3, axis=2)
    if wb is not None or adj.exposure or tt is not None:
        table = _linear_stage(table, wb, adj.exposure, tt)
    tonal = bool(adj.shadows or adj.highlights)
    table = table.astype(np.float32)
    if not tonal:                                    # nothing cross-channel before them
        _brightness_contrast_gamma(table, adj)
    img = cv2.LUT(rgb, np.ascontiguousarray(table))   # float32 0..1, the only full-size copy

    # 2. Cross-channel and spatial steps, in place on the float32 buffer.
    if tonal:
        _shadows_highlights(img, adj)
        _brightness_contrast_gamma(img, adj)
    if adj.saturation or adj.hue:
        img = _saturation_hue(img, adj)
    if adj.illumination_correction:
        gain = illumination_gain(img)
        img *= gain[..., None]
        del gain
        np.clip(img, 0.0, 1.0, out=img)
    if adj.local_contrast:
        img = _clahe(img, adj.local_contrast)
    if adj.denoise:
        # fastNlMeans only accepts 8-bit images: the image is quantized once here.
        h = float(adj.denoise)
        u8 = cv2.fastNlMeansDenoisingColored(_to_uint8(img), None, h, h, 7, 21)
        img = u8.astype(np.float32)
        del u8
        img /= 255.0
    if adj.sharpness:
        _sharpen(img, adj.sharpness / 100.0, adj.sharpen_radius)

    # 3. Output, grey or binary when asked.
    if adj.grayscale or adj.binarization != Binarization.NONE:
        gray = _to_uint8(_luma(img))
        del img
        if adj.binarization == Binarization.OTSU:
            gray = binarize_otsu(gray)
        elif adj.binarization == Binarization.SAUVOLA:
            gray = binarize_sauvola(gray)
        return cv2.cvtColor(gray, cv2.COLOR_GRAY2RGB)
    return _to_uint8(img)


def _feather_mask(h: int, w: int, feather: float) -> np.ndarray | None:
    """Blend weight (h, w, 1) rising linearly from the region's edge over feather · short side."""
    ramp = feather * min(h, w)
    if ramp < 1:
        return None
    ys = np.minimum(np.arange(h), np.arange(h)[::-1]).astype(np.float32) + 0.5
    xs = np.minimum(np.arange(w), np.arange(w)[::-1]).astype(np.float32) + 0.5
    alpha = np.minimum(ys[:, None], xs[None, :]) / ramp
    np.clip(alpha, 0.0, 1.0, out=alpha)
    return alpha[..., None]


def apply_local_adjustments(rgb: np.ndarray, locals: Iterable[LocalAdjustment], target: AdjustmentTarget,
                            reference: np.ndarray | None = None) -> np.ndarray:
    """Apply the rectangle corrections meant for `target` (or BOTH), in list order.

    Each enabled correction is computed on the cut-out of its region only (statistics such as
    white balance or illumination are those of the region). Pixels outside every rectangle are
    strictly unchanged. `restore_original` copies the region from `reference` (the geometry
    frame without any correction), which must have the same shape. `feather` blends the edge.
    Returns `rgb` itself when nothing applies; `rgb` is never modified.
    """
    _check_rgb(rgb)
    target = AdjustmentTarget(target)
    out = None
    h, w = rgb.shape[:2]
    for loc in locals:
        if not loc.enabled or loc.target not in (target, AdjustmentTarget.BOTH):
            continue
        left, top, right, bottom = norm_to_px(loc.box, w, h)
        current = rgb if out is None else out
        region = current[top:bottom, left:right]
        if loc.restore_original:
            if reference is None:
                raise ValueError("restore_original demande l'image de référence (cadre sans correction)")
            if reference.shape != rgb.shape:
                raise ValueError("l'image de référence n'a pas la taille du cadre")
            new = reference[top:bottom, left:right]
        else:
            new = apply_adjustments(np.ascontiguousarray(region), loc.adjustments)
        if new is region or np.array_equal(new, region):
            continue
        alpha = _feather_mask(bottom - top, right - left, loc.feather)
        if alpha is not None:
            blended = region.astype(np.float32)
            blended += (new.astype(np.float32) - blended) * alpha
            new = np.clip(blended + 0.5, 0, 255).astype(np.uint8)
        if out is None:
            out = rgb.copy()
        out[top:bottom, left:right] = new
    return rgb if out is None else out
