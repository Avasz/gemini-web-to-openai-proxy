"""Image input and output handling (SRS 2.5).

Input:  remote URL (fetched server-side) or inline ``data:`` URL (decoded). The
        MIME type is determined from the file's own magic bytes, never from a
        caller-supplied type or extension. Images are written to real temp files
        with the correct extension before upload, because ``gemini_webapi``'s
        upload path derives the content-type from the filename, not the bytes.

Output: images referenced in a Gemini reply are downloaded through the
        authenticated session and returned base64-encoded directly in the
        response (a Google-hosted URL is useless to an external caller).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from .translation import ImageRef

logger = logging.getLogger("gemini_proxy.media")

DEFAULT_FETCH_TIMEOUT = 20.0
DEFAULT_MAX_BYTES = 20 * 1024 * 1024

# Some image hosts (Wikipedia, CDNs) 403 a bare client; present a normal UA.
_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
}

# (mime, extension). Order matters only for prefixes that could overlap.
_MIME_BY_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/avif": ".avif",
    "image/heic": ".heic",
}


def sniff_mime(data: bytes) -> tuple[str, str] | None:
    """Return ``(mime, extension)`` from magic bytes, or ``None`` if unrecognized."""
    if len(data) < 12:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    if data[:2] == b"BM":
        return "image/bmp", ".bmp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff", ".tiff"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif", ".avif"
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "image/heic", ".heic"
    return None


@dataclass
class ImageBytes:
    data: bytes
    mime: str
    ext: str
    source: str  # "url" or "data-url"


class ImageError(ValueError):
    """A supplied image could not be fetched or decoded."""


def _decode_data_url(url: str, max_bytes: int) -> bytes:
    # data:[<mime>][;base64],<payload>
    try:
        header, _, payload = url[len("data:"):].partition(",")
    except Exception as exc:  # noqa: BLE001
        raise ImageError(f"malformed data URL: {exc}") from exc
    if not payload:
        raise ImageError("empty data URL payload")
    if "base64" in header:
        try:
            raw = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ImageError(f"invalid base64 in data URL: {exc}") from exc
    else:
        raw = httpx.URL(url).path.encode()  # percent-decoded text payload
    if len(raw) > max_bytes:
        raise ImageError(f"image exceeds max size ({len(raw)} > {max_bytes} bytes)")
    return raw


async def _fetch_url(url: str, client: httpx.AsyncClient, max_bytes: int) -> bytes:
    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ImageError(f"could not fetch image {url!r}: {exc}") from exc
    body = resp.content
    if len(body) > max_bytes:
        raise ImageError(f"image at {url!r} exceeds max size ({len(body)} bytes)")
    return body


async def resolve_image(
    ref: ImageRef,
    *,
    client: httpx.AsyncClient,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ImageBytes:
    url = ref.url
    if url.startswith("data:"):
        raw = _decode_data_url(url, max_bytes)
        source = "data-url"
    elif url.startswith(("http://", "https://")):
        raw = await _fetch_url(url, client, max_bytes)
        source = "url"
    else:
        raise ImageError(f"unsupported image reference: {url[:60]!r}")

    sniffed = sniff_mime(raw)
    if sniffed is None:
        raise ImageError("unrecognized image format (magic-byte sniff failed)")
    mime, ext = sniffed
    return ImageBytes(data=raw, mime=mime, ext=ext, source=source)


class PreparedInputImages:
    """Async context manager: fetches/decodes ``ImageRef``s, writes them to real
    temp files with correct extensions, and cleans the temp dir up on exit.

    ``.paths`` is the list to hand to ``generate_content(files=...)``.
    ``.errors`` collects per-image failures (non-fatal: a bad image is skipped).
    """

    def __init__(
        self,
        refs: list[ImageRef],
        *,
        fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ):
        self._refs = refs
        self._fetch_timeout = fetch_timeout
        self._max_bytes = max_bytes
        self._tmpdir: str | None = None
        self.paths: list[Path] = []
        self.errors: list[str] = []

    async def __aenter__(self) -> "PreparedInputImages":
        if not self._refs:
            return self
        self._tmpdir = tempfile.mkdtemp(prefix="gemini-proxy-img-")
        limits = httpx.Timeout(self._fetch_timeout)
        async with httpx.AsyncClient(
            timeout=limits, follow_redirects=True, headers=_FETCH_HEADERS
        ) as client:
            results = await asyncio.gather(
                *(
                    resolve_image(ref, client=client, max_bytes=self._max_bytes)
                    for ref in self._refs
                ),
                return_exceptions=True,
            )
        for ref, result in zip(self._refs, results):
            if isinstance(result, Exception):
                msg = f"{ref.url[:60]!r}: {result}"
                self.errors.append(msg)
                logger.warning("skipping input image %s", msg)
                continue
            fname = Path(self._tmpdir) / f"{uuid.uuid4().hex}{result.ext}"
            fname.write_bytes(result.data)
            self.paths.append(fname)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._tmpdir:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None


@dataclass
class OutputImage:
    mime_type: str
    data: str  # base64
    source_url: str | None = None


async def encode_output_images(images: list) -> list[OutputImage]:
    """Download each image a Gemini reply referenced and base64-encode it.

    Uses each ``Image`` object's own ``save()`` (which carries the authenticated
    session), to a temp dir, then reads the bytes back. Failures are skipped.
    """
    if not images:
        return []
    out: list[OutputImage] = []
    tmpdir = tempfile.mkdtemp(prefix="gemini-proxy-out-")
    try:
        for img in images:
            try:
                saved = await img.save(path=tmpdir, verbose=False)
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not download output image %s: %s",
                               getattr(img, "url", "?"), exc)
                continue
            raw = Path(saved).read_bytes()
            sniffed = sniff_mime(raw)
            mime = sniffed[0] if sniffed else "image/png"
            out.append(
                OutputImage(
                    mime_type=mime,
                    data=base64.b64encode(raw).decode("ascii"),
                    source_url=getattr(img, "url", None),
                )
            )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out
