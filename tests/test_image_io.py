"""Entrées/sorties d'images : formats acceptés, refus, protection des originaux."""
import io

import numpy as np
import pytest
from PIL import Image

from models.errors import FileTooLargeError, ImageReadError, ProtectedPathError, UnsupportedFileError
from services import image_service


def _encode(array, fmt, **kwargs) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format=fmt, **kwargs)
    return buffer.getvalue()


@pytest.fixture
def rgb():
    array = np.zeros((60, 40, 3), np.uint8)
    array[:30] = (200, 10, 10)
    return array


@pytest.mark.parametrize("fmt,name", [("JPEG", "a.jpg"), ("PNG", "a.png"), ("TIFF", "a.tif")])
def test_inspect_and_decode_images(rgb, fmt, name):
    data = _encode(rgb, fmt)
    items = image_service.inspect_upload(name, data)
    assert items == [image_service.SourceItem(0, 40, 60, "image")]
    decoded = image_service.decode_image(data)
    assert decoded.shape == (60, 40, 3) and decoded.dtype == np.uint8


def test_multipage_tiff(rgb):
    frames = [Image.fromarray(rgb), Image.fromarray(255 - rgb)]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:])
    items = image_service.inspect_upload("multi.tiff", buffer.getvalue())
    assert [item.index for item in items] == [0, 1]
    second = image_service.decode_image(buffer.getvalue(), 1)
    assert second[0, 0, 0] == 55


def test_exif_orientation_applied(rgb):
    image = Image.fromarray(rgb)
    exif = image.getexif()
    exif[274] = 6    # rotation 90° horaire à l'affichage
    data = _encode(rgb, "JPEG", exif=exif.tobytes())
    assert image_service.decode_image(data).shape == (40, 60, 3)


def test_pdf_pages_are_rasterised(tmp_path):
    import pymupdf

    document = pymupdf.open()
    for text in ("un", "deux"):
        page = document.new_page(width=200, height=300)
        page.insert_text((20, 50), text)
    data = document.tobytes()
    items = image_service.inspect_upload("livre.pdf", data)
    assert len(items) == 2 and items[0].kind == "pdf"
    path = tmp_path / "livre.pdf"
    path.write_bytes(data)
    image = image_service.read_image(path, 1)
    assert image.shape[2] == 3 and image.shape[0] > image.shape[1]


def test_rejections(rgb, monkeypatch):
    with pytest.raises(UnsupportedFileError):
        image_service.inspect_upload("a.gif", b"GIF89a")
    with pytest.raises(ImageReadError):
        image_service.inspect_upload("a.jpg", b"pas une image")
    with pytest.raises(UnsupportedFileError):
        image_service.inspect_upload("vide.png", b"")
    monkeypatch.setattr(image_service.config, "MAX_FILE_MB", 0)
    with pytest.raises(FileTooLargeError):
        image_service.inspect_upload("a.png", _encode(rgb, "PNG"))


def test_write_png_refuses_originals(tmp_path, rgb):
    with pytest.raises(ProtectedPathError):
        image_service.write_png(rgb, tmp_path / "originals" / "x.png")
    path = image_service.write_png(rgb, tmp_path / "corrected" / "x.png")
    assert np.array_equal(np.asarray(Image.open(path)), rgb)


def test_downscale_and_thumbnail(rgb):
    small, scale = image_service.downscale(rgb, 30)
    assert max(small.shape[:2]) == 30 and scale == pytest.approx(0.5)
    same, scale = image_service.downscale(rgb, 1000)
    assert same is rgb and scale == 1.0
