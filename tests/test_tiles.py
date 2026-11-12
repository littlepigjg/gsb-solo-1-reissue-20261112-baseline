import io
import numpy as np
import pytest
from PIL import Image
from backend.tiles import TILE_SIZE, PREVIEW_EDGE, level_dimensions, downscale_rgba


def open_png(response):
    return Image.open(io.BytesIO(response.data))


def test_pyramid_metadata(client, uploaded):
    pyramid = client.get(f"/api/assets/{uploaded['id']}/pyramid").json["data"]
    assert pyramid["width"] == 384 and pyramid["height"] == 320
    assert pyramid["tile_size"] == TILE_SIZE
    assert pyramid["full_bytes"] == uploaded["bytes"]
    levels = pyramid["levels"]
    assert len(levels) == 2
    assert (levels[0]["width"], levels[0]["height"]) == (384, 320)
    assert (levels[0]["cols"], levels[0]["rows"]) == (2, 2)
    assert (levels[1]["width"], levels[1]["height"]) == (192, 160)
    assert (levels[1]["cols"], levels[1]["rows"]) == (1, 1)
    assert pyramid["preview"] == {"width": 384, "height": 320}


def test_level_zero_tiles_reassemble_exactly(client, uploaded, sample_image):
    expected = np.asarray(sample_image.convert("RGBA"))
    assembled = np.zeros((320, 384, 4), dtype=np.uint8)
    for y in range(2):
        for x in range(2):
            response = client.get(f"/api/assets/{uploaded['id']}/tiles/0/{x}/{y}")
            assert response.status_code == 200
            assert response.mimetype == "image/png"
            tile = np.asarray(open_png(response).convert("RGBA"))
            assembled[y * TILE_SIZE:y * TILE_SIZE + tile.shape[0],
                      x * TILE_SIZE:x * TILE_SIZE + tile.shape[1]] = tile
    np.testing.assert_array_equal(assembled, expected)


def test_edge_tile_dimensions_and_pixels(client, uploaded):
    edge = open_png(client.get(f"/api/assets/{uploaded['id']}/tiles/0/1/1"))
    assert edge.size == (384 - TILE_SIZE, 320 - TILE_SIZE)
    assert edge.mode == "RGBA"
    # tile pixel (10, 10) maps to source pixel (266, 266) of the known gradient
    assert edge.getpixel((10, 10)) == (266 % 256, 266 % 256, (266 + 266) % 256, 255)


def test_downscaled_level_keeps_transparent_edges_clean(client, uploaded):
    tile = open_png(client.get(f"/api/assets/{uploaded['id']}/tiles/1/0/0")).convert("RGBA")
    assert tile.size == (192, 160)
    # The top source rows are fully transparent; premultiplied resizing must
    # keep their color channels at zero instead of bleeding hidden RGB out.
    assert tile.getpixel((5, 0)) == (0, 0, 0, 0)
    assert tile.getpixel((100, 1))[3] == 0
    assert tile.getpixel((100, 100))[3] == 255


def test_downscale_rgba_unpremultiplies_safely():
    image = Image.new("RGBA", (64, 64), (0, 0, 255, 0))
    for x in range(32, 64):
        for y in range(64):
            image.putpixel((x, y), (255, 0, 0, 255))
    result = downscale_rgba(image, (32, 32))
    assert result.getpixel((4, 16)) == (0, 0, 0, 0)
    boundary_red = result.getpixel((15, 16))
    assert boundary_red[0] > 150  # not diluted by the fully transparent blue
    assert result.getpixel((31, 16)) == (255, 0, 0, 255)


def test_preview_downscales_large_images(client):
    width, height = 1600, 1200
    x, y = np.meshgrid(np.arange(width), np.arange(height))
    pixels = np.stack([x % 256, y % 256, (x * y) % 256, np.full_like(x, 255)], axis=-1).astype(np.uint8)
    buffer = io.BytesIO()
    Image.fromarray(pixels).save(buffer, "PNG")
    buffer.seek(0)
    asset = client.post("/api/assets", data={"file": (buffer, "大图.png")}).json["data"]
    pyramid = client.get(f"/api/assets/{asset['id']}/pyramid").json["data"]
    assert len(pyramid["levels"]) == 4
    preview = open_png(client.get(f"/api/assets/{asset['id']}/preview"))
    assert max(preview.size) == PREVIEW_EDGE
    assert preview.size == (1024, 768)
    tile = open_png(client.get(f"/api/assets/{asset['id']}/tiles/3/0/0"))
    assert tile.size == (200, 150)


def test_preview_of_small_image_is_the_original(client, uploaded):
    response = client.get(f"/api/assets/{uploaded['id']}/preview")
    assert response.status_code == 200
    assert open_png(response).size == (384, 320)


def test_tile_requests_are_validated(client, uploaded):
    assert client.get(f"/api/assets/{uploaded['id']}/tiles/9/0/0").status_code == 400
    assert client.get(f"/api/assets/{uploaded['id']}/tiles/0/2/0").status_code == 400
    assert client.get(f"/api/assets/{uploaded['id']}/tiles/0/0/2").status_code == 400
    assert client.get(f"/api/assets/{uploaded['id']}/tiles/0/-1/0").status_code == 404
    assert client.get("/api/assets/not-an-id/tiles/0/0/0").status_code == 400
    missing = "f" * 32
    assert client.get(f"/api/assets/{missing}/tiles/0/0/0").status_code == 404
    assert client.get(f"/api/assets/{missing}/pyramid").status_code == 404


def test_tiles_are_cached_on_disk_and_conditional(client, uploaded, app):
    first = client.get(f"/api/assets/{uploaded['id']}/tiles/0/0/0")
    assert first.status_code == 200
    etag = first.headers["ETag"]
    assert client.get(f"/api/assets/{uploaded['id']}/tiles/0/0/0",
                      headers={"If-None-Match": etag}).status_code == 304
    cache_dir = app.extensions["studio"]["settings"].cache
    assert (cache_dir / "tiles" / uploaded["id"] / "0_0_0.png").is_file()


def test_cache_is_removed_with_asset(client, uploaded, app):
    client.get(f"/api/assets/{uploaded['id']}/tiles/0/0/0")
    client.get(f"/api/assets/{uploaded['id']}/preview")
    cache_dir = app.extensions["studio"]["settings"].cache
    assert (cache_dir / "tiles" / uploaded["id"]).is_dir()
    assert client.delete(f"/api/assets/{uploaded['id']}").status_code == 200
    assert not (cache_dir / "tiles" / uploaded["id"]).exists()
    assert not (cache_dir / "previews" / f"{uploaded['id']}.png").exists()


def test_tile_cache_does_not_pollute_media_diagnostics(client, uploaded):
    client.get(f"/api/assets/{uploaded['id']}/tiles/0/0/0")
    client.get(f"/api/assets/{uploaded['id']}/tiles/1/0/0")
    diagnostics = client.get("/api/diagnostics").json["data"]
    assert diagnostics["healthy"] is True
    assert diagnostics["media_file_count"] == 2
    assert diagnostics["tile_cache_files"] == 2
    assert diagnostics["tile_cache_bytes"] > 0


def test_level_dimensions_round_up():
    assert level_dimensions(385, 320, 1) == (193, 160)
    assert level_dimensions(384, 320, 2) == (96, 80)
    assert level_dimensions(1, 1, 5) == (1, 1)


def test_viewer_script_is_served(client):
    assert client.get("/static/viewer.js").status_code == 200
