"""Images : lecture et inspection des fichiers importés, écriture protégée, vignettes, réglages.

Images en mémoire : numpy RGB uint8 (H, W, 3) ou gris uint8 (H, W).
Section 1 (entrées/sorties) : fondations. Section 2 (amélioration, ombres, image OCR, découpes) : ocr-image-specialist.
"""
from __future__ import annotations

import io
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

import config
from models.errors import FileTooLargeError, ImageReadError, ProtectedPathError, UnsupportedFileError
from models.geometry import Box
from models.page import ImageEnhancement

Image.MAX_IMAGE_PIXELS = config.MAX_IMAGE_PIXELS
ORIGINALS_DIRNAME = "originals"

# --------------------------------------------------------------------------- 1. entrées / sorties


@dataclass(frozen=True)
class SourceItem:
    """Une page contenue dans un fichier importé."""

    index: int            # rang dans le fichier (page PDF, image TIFF)
    width: int
    height: int
    kind: str             # "image" ou "pdf"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extension(name: str) -> str:
    return Path(name).suffix.lower()


def inspect_upload(name: str, data: bytes) -> list[SourceItem]:
    """Vérifie extension, taille et format réel ; renvoie les pages contenues. Lève une BookOcrError explicite."""
    extension = _extension(name)
    if extension not in config.ALLOWED_EXTENSIONS:
        raise UnsupportedFileError(
            f"« {name} » : format non pris en charge (acceptés : JPG, PNG, TIFF, PDF).")
    if len(data) > config.MAX_FILE_MB * 1024 * 1024:
        raise FileTooLargeError(f"« {name} » dépasse la taille maximale de {config.MAX_FILE_MB} Mo.")
    if not data:
        raise UnsupportedFileError(f"« {name} » est vide.")
    if extension == ".pdf":
        return _pdf_items(name, data)
    return _image_items(name, data)


def _image_items(name: str, data: bytes) -> list[SourceItem]:
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "TIFF", "MPO"}:
                raise UnsupportedFileError(f"« {name} » n'est pas une image JPG, PNG ou TIFF valide.")
            frames = getattr(image, "n_frames", 1)
            items = []
            for index in range(frames):
                image.seek(index)
                width, height = ImageOps.exif_transpose(image).size if index == 0 else image.size
                if width * height > config.MAX_IMAGE_PIXELS:
                    raise FileTooLargeError(f"« {name} » : image trop grande ({width} × {height} pixels).")
                items.append(SourceItem(index, width, height, "image"))
                if image.format != "TIFF":
                    break
            return items
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ImageReadError(f"« {name} » est illisible ou endommagé.", detail=str(error)) from error


def _pdf_items(name: str, data: bytes) -> list[SourceItem]:
    import pymupdf

    try:
        document = pymupdf.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError) as error:
        raise ImageReadError(f"« {name} » n'est pas un PDF lisible.", detail=str(error)) from error
    with document:
        if document.needs_pass:
            raise UnsupportedFileError(f"« {name} » est protégé par un mot de passe.")
        if document.page_count == 0:
            raise UnsupportedFileError(f"« {name} » ne contient aucune page.")
        if document.page_count > config.MAX_PDF_PAGES_PER_FILE:
            raise FileTooLargeError(
                f"« {name} » contient {document.page_count} pages (maximum {config.MAX_PDF_PAGES_PER_FILE}).")
        zoom = config.PDF_IMPORT_DPI / 72.0
        return [SourceItem(page.number, int(page.rect.width * zoom), int(page.rect.height * zoom), "pdf")
                for page in document]


def decode_image(data: bytes, source_index: int = 0, name: str = "image") -> np.ndarray:
    """Décode une image (orientation EXIF appliquée) en RGB uint8."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if source_index:
                image.seek(source_index)
            if source_index == 0:
                image = ImageOps.exif_transpose(image)
            return _to_rgb_array(image)
    except (UnidentifiedImageError, OSError, EOFError, Image.DecompressionBombError) as error:
        raise ImageReadError(f"« {name} » est illisible.", detail=str(error)) from error


def _to_rgb_array(image: Image.Image) -> np.ndarray:
    if image.mode in ("I;16", "I;16B", "I"):
        array = np.asarray(image, dtype=np.float32)
        array = (array / max(float(array.max()), 1.0) * 255).astype(np.uint8)
        return np.ascontiguousarray(np.stack([array] * 3, axis=-1))
    if image.mode != "RGB":
        background = Image.new("RGB", image.size, (255, 255, 255))
        converted = image.convert("RGBA")
        background.paste(converted, mask=converted.getchannel("A"))
        image = background
    return np.ascontiguousarray(np.asarray(image, dtype=np.uint8))


def render_pdf_page(data: bytes, index: int, dpi: int | None = None) -> np.ndarray:
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as document:
        page = document[index]
        pixmap = page.get_pixmap(dpi=dpi or config.PDF_IMPORT_DPI, colorspace="rgb", alpha=False)
        array = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(pixmap.height, pixmap.width, 3)
        return array.copy()


def read_image(path: Path, source_index: int = 0) -> np.ndarray:
    """Lit un fichier source (image ou PDF) et renvoie la page `source_index` en RGB uint8."""
    try:
        data = Path(path).read_bytes()
    except OSError as error:
        raise ImageReadError(f"Fichier introuvable ou illisible : {Path(path).name}.", detail=str(error)) from error
    if _extension(str(path)) == ".pdf":
        return render_pdf_page(data, source_index)
    return decode_image(data, source_index, Path(path).name)


def _is_protected(path: Path) -> bool:
    return ORIGINALS_DIRNAME in Path(path).resolve().parts


def write_png(image: np.ndarray, path: Path) -> Path:
    """Écrit un PNG sans perte. Refuse d'écrire dans un dossier `originals/`. Écriture atomique."""
    path = Path(path)
    if _is_protected(path):
        raise ProtectedPathError("Écriture interdite dans le dossier des originaux.", detail=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.png")
    Image.fromarray(image).save(temporary, format="PNG", compress_level=3)
    temporary.replace(path)
    return path


def write_jpeg(image: np.ndarray, path: Path, quality: int = 85) -> Path:
    path = Path(path)
    if _is_protected(path):
        raise ProtectedPathError("Écriture interdite dans le dossier des originaux.", detail=str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp.jpg")
    Image.fromarray(image).convert("RGB").save(temporary, format="JPEG", quality=quality)
    temporary.replace(path)
    return path


def downscale(image: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    """Copie réduite (côté max `max_side`) et facteur appliqué (≤ 1)."""
    height, width = image.shape[:2]
    scale = min(1.0, max_side / max(height, width))
    if scale >= 1.0:
        return image, 1.0
    resized = cv2.resize(image, (max(1, int(width * scale)), max(1, int(height * scale))),
                         interpolation=cv2.INTER_AREA)
    return resized, scale


def thumbnail(image: np.ndarray, max_side: int = config.THUMBNAIL_MAX_SIDE) -> np.ndarray:
    return downscale(image, max_side)[0]


def to_gray(image: np.ndarray) -> np.ndarray:
    return image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)


def encode_png(image: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- 2. amélioration et préparation
# Toutes les fonctions renvoient un NOUVEAU tableau : l'entrée n'est jamais modifiée en place.
# Les statistiques (fond du papier, illustrations, balance des blancs) sont mesurées sur une copie réduite.

WORK_SIDE = 1000                  # côté maximal des copies d'analyse
MAX_ILLUMINATION_GAIN = 2.5       # un éclaircissement local ne dépasse jamais ce facteur
OCR_PAPER_LEVEL = 245.0           # niveau du papier dans l'image envoyée à l'OCR
_TO_YCC = np.array([[0.299, 0.587, 0.114],
                    [-0.168736, -0.331264, 0.5],
                    [0.5, -0.418688, -0.081312]], np.float64)


def _odd(value: float, minimum: int = 3) -> int:
    value = max(minimum, int(round(value)))
    return value if value % 2 else value + 1


def _check_rgb(rgb: np.ndarray) -> None:
    if not isinstance(rgb, np.ndarray) or rgb.dtype != np.uint8 or rgb.ndim != 3 or rgb.shape[2] != 3:
        raise ValueError("image RGB uint8 (H, W, 3) attendue")


def _to_rgb(image: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(image, cv2.COLOR_GRAY2RGB) if image.ndim == 2 else image


# ----------------------------------------------------------------- fond du papier et illustrations


def _paper_surface(lightness: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(surface quadratique du papier, pixels de papier) d'une clarté réduite (float32).

    Les lettres sont effacées par une fermeture, puis une surface quadratique est ajustée à l'enveloppe
    haute de la page (itérations : les pixels nettement sous la surface sont écartés).
    """
    h, w = lightness.shape
    closed = cv2.morphologyEx(lightness, cv2.MORPH_CLOSE, np.ones((_odd(max(h, w) / 50),) * 2, np.uint8))
    step = max(1, int(np.sqrt(h * w / 8000)))
    grid_y, grid_x = np.mgrid[0:h:step, 0:w:step]
    values = closed[::step, ::step].ravel().astype(np.float64)
    x = grid_x.ravel() / max(w - 1, 1) * 2 - 1
    y = grid_y.ravel() / max(h - 1, 1) * 2 - 1
    design = np.stack([np.ones_like(x), x, y, x * x, x * y, y * y], axis=1)
    top = max(float(np.percentile(values, 95)), 1e-3)
    keep = values >= 0.5 * top
    coefficients = None
    for _ in range(6):
        if keep.sum() < 30:
            break
        coefficients = np.linalg.lstsq(design[keep], values[keep], rcond=None)[0]
        new_keep = values >= 0.92 * (design @ coefficients)
        if np.array_equal(new_keep, keep):
            break
        keep = new_keep
    if coefficients is None:
        surface = np.full((h, w), top, np.float32)
    else:
        yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
        xx = xx / max(w - 1, 1) * 2 - 1
        yy = yy / max(h - 1, 1) * 2 - 1
        c = coefficients
        surface = (c[0] + c[1] * xx + c[2] * yy + c[3] * xx * xx + c[4] * xx * yy + c[5] * yy * yy).astype(np.float32)
    np.clip(surface, max(0.3 * top, 1e-3), None, out=surface)
    paper = closed >= 0.92 * surface
    if paper.sum() < 10:
        paper = np.ones_like(paper)
    return surface, paper


def _picture_mask_small(small_rgb: np.ndarray) -> np.ndarray:
    """Photos et illustrations (bool) d'une copie RGB uint8 déjà réduite.

    Les lettres et filets sont retirés par une ouverture des pixels « non papier » ; une grande zone
    restante est une illustration si elle est colorée, très nuancée ou largement sombre.
    """
    lab = cv2.cvtColor(small_rgb.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    lightness = lab[..., 0]
    surface, paper = _paper_surface(lightness)
    relative = np.clip(lightness / surface * 100.0, 0, 100)
    a0, b0 = (float(np.median(lab[..., channel][paper])) for channel in (1, 2))
    chroma = np.hypot(lab[..., 1] - a0, lab[..., 2] - b0)
    non_paper = ((relative < 88) | (chroma > 18)).astype(np.uint8)
    h, w = lightness.shape
    size = _odd(max(15, min(h, w) // 40))
    bridged = cv2.morphologyEx(non_paper, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    large = cv2.morphologyEx(bridged, cv2.MORPH_OPEN, np.ones((size, size), np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(large, connectivity=8)
    mask = np.zeros((h, w), np.uint8)
    smooth = cv2.medianBlur(np.clip(relative * 2.55, 0, 255).astype(np.uint8), 9)
    for index in range(1, count):
        area = stats[index][4]
        if area < 0.01 * h * w:
            continue
        region = labels == index
        tones = smooth[region]
        colourful = float(np.median(chroma[region])) > 15
        varied = np.percentile(tones, 90) - np.percentile(tones, 10) >= 55
        dark = float(np.median(relative[region])) < 50 and area >= 0.02 * h * w
        if colourful or varied or dark:
            mask[region] = 1
    if mask.any():
        mask = cv2.dilate(mask, np.ones((5, 5), np.uint8))
    return mask.astype(bool)


def picture_mask(rgb: np.ndarray) -> np.ndarray:
    """Pixels (bool, pleine taille) appartenant à des photos ou illustrations (estimation)."""
    rgb = _to_rgb(rgb)
    _check_rgb(rgb)
    small, scale = downscale(rgb, WORK_SIDE)
    mask = _picture_mask_small(small)
    if scale >= 1.0:
        return mask
    return cv2.resize(mask.astype(np.uint8), (rgb.shape[1], rgb.shape[0]),
                      interpolation=cv2.INTER_NEAREST).astype(bool)


def picture_share(rgb: np.ndarray) -> float:
    """Part (0..1) de la page couverte par des illustrations (copie réduite)."""
    small, _ = downscale(_to_rgb(rgb), WORK_SIDE)
    return float(_picture_mask_small(small).mean())


def paper_background(rgb: np.ndarray) -> np.ndarray:
    """Clarté du papier estimée (float32 0..1, pleine taille) : l'éclairage de la page sans le contenu.

    Mesurée sur une copie réduite : une fermeture efface les lettres, seuls les pixels « papier » (clairs,
    hors illustrations) sont gardés et les trous sont comblés par convolution normalisée à échelles
    croissantes. Sous une grande illustration, le fond est interpolé depuis le papier voisin.
    """
    rgb = _to_rgb(rgb)
    height, width = rgb.shape[:2]
    small, _ = downscale(rgb, 600)
    small_h, small_w = small.shape[:2]
    luma = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY).astype(np.float32) / 255.0
    side = max(small_h, small_w)
    closed = cv2.morphologyEx(luma, cv2.MORPH_CLOSE, np.ones((_odd(side / 50),) * 2, np.uint8))
    paper_level = max(float(np.percentile(closed, 90)), 1e-3)
    pictures = _picture_mask_small(small)
    mask = ((closed >= 0.5 * paper_level) & ~pictures).astype(np.float32)
    values = closed * mask
    background = np.full((small_h, small_w), paper_level, np.float32)
    filled = np.zeros((small_h, small_w), bool)
    sigma = side / 25.0
    for _ in range(5):
        numerator = cv2.GaussianBlur(values, (0, 0), sigma)
        denominator = cv2.GaussianBlur(mask, (0, 0), sigma)
        usable = (denominator > 0.05) & ~filled
        background[usable] = numerator[usable] / denominator[usable]
        filled |= usable
        if filled.all():
            break
        sigma *= 2.0
    background = cv2.GaussianBlur(background, (0, 0), side / 100.0)
    np.clip(background, 1.0 / 255.0, 1.0, out=background)
    if (small_h, small_w) != (height, width):
        background = cv2.resize(background, (width, height), interpolation=cv2.INTER_LINEAR)
    return background


def _apply_gain(rgb: np.ndarray, gain: np.ndarray) -> np.ndarray:
    """Multiplie chaque canal par une carte de gain (H, W) ; arrondi et bornage uint8."""
    out = rgb.astype(np.float32)
    out *= gain[..., None]
    out += 0.5
    np.clip(out, 0, 255, out=out)
    return out.astype(np.uint8)


def correct_shadows(rgb: np.ndarray, strength: float) -> np.ndarray:
    """Égalise l'éclairage du papier (division par le fond estimé). `strength` de 0 (rien) à 1 (complet).

    Le papier est ramené à son niveau clair habituel (95e centile du fond), pas au blanc pur ;
    les illustrations reçoivent le gain du papier qui les entoure (elles ne sont pas blanchies).
    """
    _check_rgb(rgb)
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength == 0.0:
        return rgb.copy()
    background = paper_background(rgb)
    reference = float(np.percentile(downscale(background, 300)[0], 95))
    gain = np.power(reference / background, strength, dtype=np.float32)
    np.clip(gain, 1.0 / MAX_ILLUMINATION_GAIN, MAX_ILLUMINATION_GAIN, out=gain)
    return _apply_gain(rgb, gain)


# ----------------------------------------------------------------- réglages photographiques


def white_balance_gains(rgb: np.ndarray) -> tuple[float, float, float]:
    """Gains (r, g, b) qui rendent le papier neutre : moyenne des 10 % de pixels les plus clairs non saturés."""
    small = downscale(rgb, 800)[0].reshape(-1, 3).astype(np.float32)
    luma = small @ np.array([0.299, 0.587, 0.114], np.float32)
    usable = small.max(axis=1) < 250
    if usable.sum() < 50:
        usable = np.ones(len(small), bool)
    threshold = np.percentile(luma[usable], 90)
    paper = small[usable & (luma >= threshold)]
    if len(paper) < 10:
        return (1.0, 1.0, 1.0)
    means = paper.mean(axis=0)
    target = float(means.mean())
    gains = [float(np.clip(target / max(float(mean), 1.0), 0.5, 2.0)) for mean in means]
    return (gains[0], gains[1], gains[2])


def _tone_table(settings: ImageEnhancement, gains: tuple[float, float, float]) -> np.ndarray:
    """Table 256 × 1 × 3 (uint8) : balance des blancs, luminosité, contraste."""
    base = np.arange(256, dtype=np.float32) / 255.0
    table = np.stack([base * gain for gain in gains], axis=-1)[:, None, :]
    if settings.brightness:
        table += settings.brightness / 100.0 * 0.25
    if settings.contrast:
        factor = 2.0 ** (settings.contrast / 50.0)       # × 4 à +100, × 0,25 à -100
        table = (table - 0.5) * factor + 0.5
    return np.clip(table * 255.0 + 0.5, 0, 255).astype(np.uint8)


def _saturate(rgb: np.ndarray, amount: float) -> np.ndarray:
    """Multiplie la chrominance (YCbCr) par 1 + amount/100 ; la luminance est conservée."""
    middle = np.eye(3)
    middle[1:, 1:] *= 1.0 + amount / 100.0
    matrix = (np.linalg.inv(_TO_YCC) @ middle @ _TO_YCC).astype(np.float32)
    out = cv2.transform(rgb.astype(np.float32), matrix)
    out += 0.5
    np.clip(out, 0, 255, out=out)
    return out.astype(np.uint8)


def _denoise(rgb: np.ndarray, amount: float) -> np.ndarray:
    """Débruitage modéré qui garde les contours (filtre bilatéral), dosé par mélange."""
    strength = amount / 100.0
    filtered = cv2.bilateralFilter(rgb, d=5, sigmaColor=15 + 45 * strength, sigmaSpace=3)
    return cv2.addWeighted(rgb, 1.0 - strength, filtered, strength, 0)


def _readability(rgb: np.ndarray, amount: float) -> np.ndarray:
    """Contraste local (CLAHE sur L de Lab). Les noirs francs sont protégés (CLAHE les grisaillerait)."""
    strength = amount / 100.0
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2Lab)
    lightness = lab[..., 0]
    equalised = cv2.createCLAHE(clipLimit=1.0 + 3.0 * strength, tileGridSize=(8, 8)).apply(lightness)
    delta = equalised.astype(np.float32) - lightness
    paper = max(float(np.percentile(downscale(lightness, 600)[0], 90)), 1.0)
    weight = np.clip((lightness.astype(np.float32) / paper - 0.25) / 0.2, 0.0, 1.0)
    new = lightness + delta * weight * min(1.0, 0.5 + strength)
    lab[..., 0] = np.clip(new + 0.5, 0, 255).astype(np.uint8)
    return cv2.cvtColor(lab, cv2.COLOR_Lab2RGB)


def _sharpen(rgb: np.ndarray, amount: float) -> np.ndarray:
    """Masque flou : image + k · (image - flou)."""
    k = 1.5 * amount / 100.0
    blurred = cv2.GaussianBlur(rgb, (0, 0), 1.2)
    return cv2.addWeighted(rgb, 1.0 + k, blurred, -k, 0)


def enhance(rgb: np.ndarray, settings: ImageEnhancement) -> np.ndarray:
    """Applique les réglages à une copie. Ordre : balance des blancs, ombres, luminosité et contraste,
    saturation, débruitage, lisibilité, netteté. Réglages neutres : copie identique. Jamais de binarisation.
    """
    _check_rgb(rgb)
    if settings.is_neutral():
        return rgb.copy()
    out = rgb
    gains = white_balance_gains(rgb) if settings.white_balance == "auto" else (1.0, 1.0, 1.0)
    if settings.shadow_correction:
        out = correct_shadows(out, settings.shadow_correction / 100.0)
    if gains != (1.0, 1.0, 1.0) or settings.brightness or settings.contrast:
        out = cv2.LUT(out, _tone_table(settings, gains))
    if settings.saturation:
        out = _saturate(out, settings.saturation)
    if settings.denoise:
        out = _denoise(out, settings.denoise)
    if settings.readability:
        out = _readability(out, settings.readability)
    if settings.sharpness:
        out = _sharpen(out, settings.sharpness)
    return out.copy() if out is rgb else out


# ----------------------------------------------------------------- image OCR, découpes, fonds


def binarize_sauvola(gray: np.ndarray, window: int | None = None, k: float = 0.2, r: float = 128.0) -> np.ndarray:
    """Seuil local de Sauvola (uint8 0/255), robuste aux fonds irréguliers.

    Dans un aplat noir la moyenne locale est aussi sombre que le bruit : tout ce qui est nettement plus
    sombre que le papier reste noir.
    """
    gray = to_gray(gray)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0).astype(np.float32)
    window = _odd(window or max(blurred.shape) // 40, 25)
    mean = cv2.boxFilter(blurred, -1, (window, window), borderType=cv2.BORDER_REPLICATE)
    squares = cv2.boxFilter(blurred * blurred, -1, (window, window), borderType=cv2.BORDER_REPLICATE)
    squares -= mean * mean
    np.maximum(squares, 0, out=squares)
    np.sqrt(squares, out=squares)
    threshold = mean * (1 + k * (squares / r - 1))
    paper = float(np.percentile(downscale(blurred, 600)[0], 90))
    return np.where((blurred > threshold) & (blurred > 0.4 * paper), 255, 0).astype(np.uint8)


def prepare_ocr_image(rgb: np.ndarray, illustration_boxes: list[Box] | tuple[Box, ...] = ()) -> np.ndarray:
    """Image grise envoyée à l'OCR : éclairage égalisé (papier ≈ 245), illustrations blanchies. Pas de binarisation."""
    rgb = _to_rgb(rgb)
    _check_rgb(rgb)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    background = paper_background(rgb)
    gain = np.clip(OCR_PAPER_LEVEL / 255.0 / background, 1.0 / MAX_ILLUMINATION_GAIN, MAX_ILLUMINATION_GAIN)
    gray *= gain
    gray += 0.5
    np.clip(gray, 0, 255, out=gray)
    out = gray.astype(np.uint8)
    height, width = out.shape
    for box in illustration_boxes:
        left, top, right, bottom = box.to_pixels(width, height)
        out[top:bottom, left:right] = 255
    return out


def crop_box(rgb: np.ndarray, box: Box, padding: float = 0.0) -> np.ndarray:
    """Découpe pleine résolution, sans perte, de `box` élargie de `padding` (fraction de la page)."""
    height, width = rgb.shape[:2]
    region = box.expanded(padding) if padding > 0 else box
    left, top, right, bottom = region.to_pixels(width, height)
    return rgb[top:bottom, left:right].copy()


def _ink_mask(crop: np.ndarray) -> np.ndarray:
    """Pixels de lettres (uint8 0/1) d'une région de texte, foncées sur clair ou claires sur foncé."""
    gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
    height, width = gray.shape
    size = _odd(np.clip(min(height, width) / 4, 5, 41))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    low, high = np.percentile(gray, [5, 95])
    threshold = max(18.0, 0.2 * float(high - low))
    if float(np.median(gray)) >= (low + high) / 2:          # texte foncé sur fond clair
        response = cv2.morphologyEx(gray, cv2.MORPH_CLOSE, kernel).astype(np.int16) - gray
    else:                                                    # texte clair sur fond foncé
        response = gray.astype(np.int16) - cv2.morphologyEx(gray, cv2.MORPH_OPEN, kernel)
    mask = (response > threshold).astype(np.uint8)
    grow = max(2, int(round(min(height, width) * 0.01)))
    return cv2.dilate(mask, np.ones((2 * grow + 1, 2 * grow + 1), np.uint8))


def _perimeter_median(rgb: np.ndarray, left: int, top: int, right: int, bottom: int) -> np.ndarray:
    """Couleur médiane d'un anneau de 3 pixels autour de la boîte (ou de son bord intérieur à défaut)."""
    height, width = rgb.shape[:2]
    ring = 3
    outer = rgb[max(0, top - ring):min(height, bottom + ring), max(0, left - ring):min(width, right + ring)]
    mask = np.ones(outer.shape[:2], bool)
    inner_top, inner_left = top - max(0, top - ring), left - max(0, left - ring)
    mask[inner_top:inner_top + bottom - top, inner_left:inner_left + right - left] = False
    pixels = outer[mask]
    if len(pixels) < 4:
        box = rgb[top:bottom, left:right]
        pixels = np.concatenate([box[0], box[-1], box[:, 0], box[:, -1]])
    return np.median(pixels.reshape(-1, 3), axis=0).astype(np.uint8)


def erase_text_regions(rgb: np.ndarray, boxes: list[Box], method: Literal["inpaint", "fill"] = "inpaint") -> np.ndarray:
    """Fond « reconstruit » (ESTIMATION) : le texte des boîtes est effacé sur une copie.

    inpaint : cv2.inpaint sur le masque d'encre dilaté, limité aux boîtes ; fill : chaque boîte est remplie
    par la couleur médiane de son pourtour. Les pixels hors des boîtes ne changent pas.
    """
    _check_rgb(rgb)
    if method not in ("inpaint", "fill"):
        raise ValueError(f"méthode d'effacement inconnue : {method}")
    out = rgb.copy()
    height, width = rgb.shape[:2]
    for box in boxes:
        left, top, right, bottom = box.to_pixels(width, height)
        if method == "fill":
            out[top:bottom, left:right] = _perimeter_median(rgb, left, top, right, bottom)
            continue
        margin = max(4, int(0.1 * max(right - left, bottom - top)))
        x0, y0 = max(0, left - margin), max(0, top - margin)
        x1, y1 = min(width, right + margin), min(height, bottom + margin)
        mask = np.zeros((y1 - y0, x1 - x0), np.uint8)
        mask[top - y0:bottom - y0, left - x0:right - x0] = _ink_mask(rgb[top:bottom, left:right])
        if not mask.any():
            continue
        radius = max(3, int(round(min(right - left, bottom - top) * 0.05)))
        patch = cv2.inpaint(np.ascontiguousarray(out[y0:y1, x0:x1]), mask, radius, cv2.INPAINT_TELEA)
        region = mask.astype(bool)
        out[y0:y1, x0:x1][region] = patch[region]
    return out
