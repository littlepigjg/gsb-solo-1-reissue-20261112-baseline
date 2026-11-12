import numpy as np
import pytest
from PIL import Image
from backend.catalog import OPERATIONS
from backend.errors import StudioError
from backend.registry import Registry, OperationContext
from backend.settings import Settings


@pytest.fixture(scope="module")
def registry():
    return Registry()


@pytest.mark.parametrize("spec", OPERATIONS, ids=[spec["id"] for spec in OPERATIONS])
def test_every_operation_runs(spec, registry, sample_image, tmp_path):
    context = OperationContext(Settings(root=tmp_path))
    output = registry.apply(sample_image, {"operation": spec["id"], "params": {}}, context)
    assert isinstance(output, Image.Image)
    assert output.mode == "RGBA"
    assert output.width > 0 and output.height > 0
    if spec["preserves_dimensions"]:
        assert output.size == sample_image.size
    if spec["category"] in {"color", "enhance", "edge"}:
        assert np.array_equal(np.asarray(output.getchannel("A")), np.asarray(sample_image.getchannel("A")))


def test_resize_exact_dimensions(registry, sample_image, tmp_path):
    output = registry.apply(sample_image, {"operation": "resize_exact", "params": {"width": 111, "height": 73}}, OperationContext(Settings(tmp_path)))
    assert output.size == (111, 73)


def test_rotate_exact_pixels(registry, sample_image, tmp_path):
    output = registry.apply(sample_image, {"operation": "transform_quarter", "params": {"angle": "90"}}, OperationContext(Settings(tmp_path)))
    assert np.array_equal(np.asarray(output), np.rot90(np.asarray(sample_image)))


def test_crop_correct_pixels(registry, sample_image, tmp_path):
    output = registry.apply(sample_image, {"operation": "crop_rect", "params": {"left": 30, "top": 40, "width": 70, "height": 80}}, OperationContext(Settings(tmp_path)))
    assert np.array_equal(np.asarray(output), np.asarray(sample_image)[40:120, 30:100])


def test_invert_is_involution(registry, sample_image, tmp_path):
    context = OperationContext(Settings(tmp_path))
    step = {"operation": "color_invert", "params": {}}
    output = registry.apply(registry.apply(sample_image, step, context), step, context)
    assert np.array_equal(np.asarray(output), np.asarray(sample_image))


def test_gamma_identity(registry, sample_image, tmp_path):
    output = registry.apply(sample_image, {"operation": "color_gamma", "params": {"gamma": 1}}, OperationContext(Settings(tmp_path)))
    assert np.max(np.abs(np.asarray(output).astype(float) - np.asarray(sample_image))) <= 1


def test_color_key_transparency(registry, tmp_path):
    image = Image.new("RGBA", (20, 20), (0, 255, 0, 255))
    image.putpixel((10, 10), (255, 0, 0, 255))
    output = registry.apply(image, {"operation": "format_color_key", "params": {}}, OperationContext(Settings(tmp_path)))
    assert output.getpixel((0, 0))[3] == 0
    assert output.getpixel((10, 10))[3] == 255


def test_crop_rejects_out_of_bounds(registry, sample_image, tmp_path):
    with pytest.raises(StudioError):
        registry.apply(sample_image, {"operation": "crop_rect", "params": {"width": 8000}}, OperationContext(Settings(tmp_path)))


@pytest.mark.parametrize("params", [{"factor": True}, {"factor": "1.2"}, {"factor": float("nan")}, {"factor": 5}, {"unexpected": 1}])
def test_strict_parameter_validation(registry, params):
    with pytest.raises(StudioError):
        registry.validate_step({"operation": "color_brightness", "params": params})


def test_odd_window_validation(registry):
    with pytest.raises(StudioError):
        registry.validate_step({"operation": "enhance_median", "params": {"size": 4}})


def test_all_builtin_examples_validate(registry):
    for spec in OPERATIONS:
        assert registry.validate_step(spec["example"])["operation"] == spec["id"]


def test_deterministic_grain(registry, sample_image, tmp_path):
    context = OperationContext(Settings(tmp_path))
    step = {"operation": "filter_grain", "params": {"seed": 14}}
    assert np.array_equal(np.asarray(registry.apply(sample_image, step, context)), np.asarray(registry.apply(sample_image, step, context)))


def test_memory_dimension_guard(registry, sample_image, tmp_path):
    with pytest.raises(StudioError):
        registry.apply(sample_image, {"operation": "resize_exact", "params": {"width": 8192, "height": 8192}}, OperationContext(Settings(tmp_path)))
