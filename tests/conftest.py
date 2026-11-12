import io
import sys
import os
from pathlib import Path
import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, os.path.relpath(Path(__file__).parents[1]))
from backend.app import create_app


@pytest.fixture
def app(tmp_path):
    application = create_app(data_dir=tmp_path / "data", testing=True)
    yield application
    application.extensions["studio"]["jobs"].shutdown()
    application.extensions["studio"]["store"].close()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sample_image():
    width, height = 384, 320
    x, y = np.meshgrid(np.arange(width), np.arange(height))
    pixels = np.zeros((height, width, 4), dtype=np.uint8)
    pixels[..., 0] = x % 256
    pixels[..., 1] = y % 256
    pixels[..., 2] = (x + y) % 256
    pixels[..., 3] = 255
    pixels[:10, :, 3] = 0
    pixels[10:20, :, 3] = 120
    return Image.fromarray(pixels)


@pytest.fixture
def uploaded(client, sample_image):
    buffer = io.BytesIO()
    sample_image.save(buffer, "PNG")
    buffer.seek(0)
    response = client.post("/api/assets", data={"file": (buffer, "测试原图.png")})
    assert response.status_code == 201, response.json
    return response.json["data"]
