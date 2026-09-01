import base64

import pytest

from app.media import PreparedInputImages, encode_output_images, sniff_mime
from app.translation import ImageRef
from tests.conftest import PNG_1PX, FakeImage


def test_sniff_common_formats():
    assert sniff_mime(PNG_1PX) == ("image/png", ".png")
    assert sniff_mime(b"\xff\xd8\xff\xe0" + b"\x00" * 20)[0] == "image/jpeg"
    assert sniff_mime(b"GIF89a" + b"\x00" * 20)[0] == "image/gif"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 4)[0] == "image/webp"
    assert sniff_mime(b"\x00\x00\x00\x18ftypavif" + b"\x00" * 8)[0] == "image/avif"
    assert sniff_mime(b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8)[0] == "image/heic"
    assert sniff_mime(b"not an image at all") is None


async def test_prepared_input_from_data_url_writes_real_file():
    data_url = "data:image/png;base64," + base64.b64encode(PNG_1PX).decode()
    async with PreparedInputImages([ImageRef(url=data_url)]) as prep:
        assert len(prep.paths) == 1
        p = prep.paths[0]
        assert p.suffix == ".png"          # extension from magic bytes
        assert p.read_bytes() == PNG_1PX
        assert prep.errors == []
        tmp_parent = p.parent
    # temp dir cleaned up on exit
    assert not tmp_parent.exists()


async def test_prepared_input_bad_image_is_skipped_not_fatal():
    async with PreparedInputImages(
        [ImageRef(url="data:image/png;base64,%%%notbase64%%%")]
    ) as prep:
        assert prep.paths == []
        assert len(prep.errors) == 1


async def test_prepared_input_rejects_unsniffable_bytes():
    data_url = "data:application/octet-stream;base64," + base64.b64encode(
        b"totally not an image"
    ).decode()
    async with PreparedInputImages([ImageRef(url=data_url)]) as prep:
        assert prep.paths == []
        assert "magic-byte sniff failed" in prep.errors[0]


async def test_encode_output_images():
    out = await encode_output_images([FakeImage(), FakeImage(url="u2")])
    assert len(out) == 2
    assert out[0].mime_type == "image/png"
    assert base64.b64decode(out[0].data) == PNG_1PX
    assert out[1].source_url == "u2"


async def test_encode_output_images_empty():
    assert await encode_output_images([]) == []
