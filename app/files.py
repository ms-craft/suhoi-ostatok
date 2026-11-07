import os
import struct
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
from tempfile import TemporaryFile
from threading import BoundedSemaphore, Lock

import pypdfium2 as pdfium
from fastapi import UploadFile
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from app.config import Settings
from app.core import ApiError

# PDFium is not thread-safe, even for different documents.
PDF_LOCK = Lock()
PDF_PROCESS_SLOTS = BoundedSemaphore(1)
PDF_TIMEOUT_SECONDS = 25
PDF_PAGE_BYTES = 12 * 1024 * 1024
PDF_OUTPUT_BYTES = 64 * 1024 * 1024
JPEG = "image/jpeg"
PNG = "image/png"
PDF = "application/pdf"
HEIC = "image/heic"
HEIF = "image/heif"
HEIF_BRANDS = frozenset({b'heic', b'heix', b'heim', b'heis', b'hevc', b'hevx', b'hevm', b'hevs', b'mif1', b'msf1'})
register_heif_opener(thumbnails=False)


@dataclass(frozen=True)
class PreparedFile:
    content: bytes
    mime_type: str
    pages: list[bytes]


def page_limit() -> ApiError:
    return ApiError(413, "page_limit", "Превышен лимит страниц работы.")


def invalid_file() -> ApiError:
    return ApiError(422, "invalid_file", "Файл повреждён или защищён паролем.")


def render_image(content: bytes, image_format: str, settings: Settings) -> bytes:
    try:
        with Image.open(BytesIO(content), formats=[image_format]) as image:
            if image.width * image.height > settings.max_image_pixels:
                limit = f'{settings.max_image_pixels:,}'.replace(',', ' ')
                raise ApiError(413, "image_pixel_limit", f"Размер изображения превышает {limit} пикселей (ширина × высота).")
            if getattr(image, "n_frames", 1) != 1:
                raise ApiError(415, "unsupported_file", "Поддерживаются только статические изображения.")
            image.verify()
        with Image.open(BytesIO(content), formats=[image_format]) as image:
            return normalized_jpeg(ImageOps.exif_transpose(image), settings.page_image_edge)
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError) as error:
        raise invalid_file() from error


def normalized_jpeg(image: Image.Image, max_edge: int) -> bytes:
    image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        layer = image.convert("RGBA")
        canvas = Image.new("RGB", layer.size, "white")
        canvas.paste(layer, mask=layer.getchannel("A"))
        image = canvas
    else:
        image = image.convert("RGB")
    output = BytesIO()
    image.save(output, format="JPEG", quality=88)
    return output.getvalue()


def render_pdf(content: bytes, max_pages: int, page_image_edge: int) -> list[bytes]:
    result = []
    try:
        with PDF_LOCK, closing(pdfium.PdfDocument(content)) as document:
            if not len(document):
                raise invalid_file()
            if len(document) > max_pages:
                raise page_limit()
            for index in range(len(document)):
                with closing(document[index]) as page:
                    edge = max(page.get_size())
                    if not 1 <= edge <= 14400:
                        raise invalid_file()
                    scale = page_image_edge / edge
                    with closing(page.render(scale=scale, draw_annots=True)) as bitmap:
                        result.append(normalized_jpeg(bitmap.to_pil(), page_image_edge))
    except (pdfium.PdfiumError, OSError, ValueError, OverflowError) as error:
        raise invalid_file() from error
    return result


def _parse_rendered_pdf(content: bytes, max_pages: int) -> list[bytes]:
    stream = BytesIO(content)
    try:
        count, = struct.unpack("!I", stream.read(4))
        if not 1 <= count <= max_pages:
            raise ValueError
        result = []
        for _ in range(count):
            size, = struct.unpack("!I", stream.read(4))
            if not 1 <= size <= PDF_PAGE_BYTES:
                raise ValueError
            image = stream.read(size)
            if len(image) != size or not image.startswith(b"\xff\xd8\xff"):
                raise ValueError
            result.append(image)
        if stream.read(1):
            raise ValueError
    except (ValueError, struct.error) as error:
        raise invalid_file() from error
    return result


def render_pilot_pdf(content: bytes, max_pages: int, page_image_edge: int) -> list[bytes]:
    if not PDF_PROCESS_SLOTS.acquire(blocking=False):
        raise ApiError(503, "pdf_renderer_busy", "Обработка PDF занята. Попробуйте позже.")
    try:
        with TemporaryFile() as output:
            child = subprocess.run(
                [sys.executable, "-m", "app.pdf_worker", str(max_pages), str(page_image_edge)],
                input=content,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=PDF_TIMEOUT_SECONDS,
                env={key: os.environ[key] for key in ("SystemRoot",) if key in os.environ},
            )
            if child.returncode == 2:
                raise page_limit()
            if child.returncode != 0 or output.tell() > PDF_OUTPUT_BYTES:
                raise invalid_file()
            output.seek(0)
            return _parse_rendered_pdf(output.read(PDF_OUTPUT_BYTES + 1), max_pages)
    except subprocess.TimeoutExpired as error:
        raise ApiError(422, "pdf_render_timeout", "Не удалось обработать PDF за отведённое время.") from error
    except OSError as error:
        raise ApiError(503, "pdf_renderer_unavailable", "Обработка PDF временно недоступна.") from error
    finally:
        PDF_PROCESS_SLOTS.release()


def prepare_uploads(
    files: list[UploadFile], settings: Settings, *, max_pages: int, required: bool = True,
) -> list[PreparedFile]:
    if required and not files:
        raise ApiError(422, "missing_files", "Добавьте фотографию или PDF.")
    if len(files) > max_pages:
        raise page_limit()

    result = []
    for file in files:
        content = file.file.read(settings.max_file_bytes + 1)
        if len(content) > settings.max_file_bytes:
            limit_mb = f'{settings.max_file_bytes / (1024 * 1024):g}'
            raise ApiError(413, "file_too_large", f"Размер файла превышает {limit_mb} МБ.")
        if content.startswith(b"%PDF-"):
            mime_type = PDF
        elif content.startswith(b"\xff\xd8\xff"):
            mime_type = JPEG
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime_type = PNG
        elif len(content) >= 12 and content[4:8] == b'ftyp' and content[8:12] in HEIF_BRANDS:
            mime_type = HEIF if content[8:12] in {b'mif1', b'msf1'} else HEIC
        else:
            raise ApiError(415, "unsupported_file", "Поддерживаются JPEG, PNG, HEIC, HEIF и PDF.")
        allowed_types = {None, "application/octet-stream", mime_type}
        if mime_type in {HEIC, HEIF}:
            allowed_types.update({HEIC, HEIF})
        if file.content_type not in allowed_types:
            raise ApiError(415, "unsupported_file", "Тип файла не соответствует его содержимому.")
        if mime_type == PDF:
            if len(files) != 1:
                raise ApiError(422, "mixed_upload", "Загрузите фотографии или один PDF.")
            render = render_pilot_pdf if settings.deployment_mode == "pilot" else render_pdf
            pages = render(content, max_pages, settings.page_image_edge)
        else:
            image_format = {JPEG: 'JPEG', PNG: 'PNG', HEIC: 'HEIF', HEIF: 'HEIF'}[mime_type]
            pages = [render_image(content, image_format, settings)]
        result.append(PreparedFile(content, mime_type, pages))
    return result
