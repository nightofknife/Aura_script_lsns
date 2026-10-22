"""Path template cache lifecycle, ownership and real inspect integration."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from plans.aura_base.src.actions import vision_actions
from plans.aura_base.src.services._template_image_cache import TemplateImageCache
from plans.aura_base.src.services.vision_service import VisionService


def write_image(path, image):
    path.write_bytes(cv2.imencode(".png", image)[1].tobytes())


@pytest.fixture
def pixels(tmp_path):
    source = np.random.default_rng(782).integers(10, 246, (40, 55, 3), dtype=np.uint8)
    template = source[7:18, 9:22].copy()
    path = tmp_path / "模板.png"
    write_image(path, cv2.cvtColor(template, cv2.COLOR_RGB2BGR))
    return source, template, path


def track_loads(monkeypatch, service):
    original = service.load_image_file
    calls = []

    def load(path, flags=cv2.IMREAD_UNCHANGED):
        calls.append(str(path))
        return original(path, flags)

    monkeypatch.setattr(service, "load_image_file", load)
    return calls


def test_reuses_decoding_across_profiles_but_not_source(monkeypatch, pixels):
    source, template, path = pixels
    service = VisionService()
    calls = track_loads(monkeypatch, service)
    for gray, preprocess in [(False, "none"), (True, "none"), (True, "edge"), (False, "blur")]:
        for _ in range(2):
            prepared = service._prepare_match_image(str(path), use_grayscale=gray,
                                                    preprocess=preprocess,
                                                    stage="template_prepare_sum", timing=None)
            expected = service._prepare_image(template, use_grayscale=gray, preprocess=preprocess)
            np.testing.assert_array_equal(prepared, expected)
    assert calls == [str(path)]
    raw = service.load_template_image(path)
    np.testing.assert_array_equal(raw, template)
    source_path = path.parent / "source.png"
    write_image(source_path, cv2.cvtColor(source, cv2.COLOR_RGB2BGR))
    for _ in range(2):
        service.find_template(str(source_path), str(path))
    assert calls.count(str(source_path)) == 2


def test_update_delete_and_recreate_invalidate_cache(pixels):
    _, template, path = pixels
    service = VisionService()
    first = service.load_template_image(path)
    old_stat = path.stat()
    replacement = 255 - template
    write_image(path, cv2.cvtColor(replacement, cv2.COLOR_RGB2BGR))
    os.utime(path, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns + 2_000_000_000))
    np.testing.assert_array_equal(service.load_template_image(path), replacement)
    np.testing.assert_array_equal(first, template)
    path.unlink()
    with pytest.raises(FileNotFoundError, match="无法从路径加载图像"):
        service.load_template_image(path)
    assert not service._template_image_cache._entries
    write_image(path, cv2.cvtColor(template, cv2.COLOR_RGB2BGR))
    np.testing.assert_array_equal(service.load_template_image(path), template)


def test_directory_link_retarget_invalidates_cached_pixels(tmp_path):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    first = np.full((5, 7, 3), 31, dtype=np.uint8)
    second = np.full((5, 7, 3), 197, dtype=np.uint8)
    write_image(first_dir / "asset.png", first)
    write_image(second_dir / "asset.png", second)
    original_time = (first_dir / "asset.png").stat().st_mtime_ns
    os.utime(second_dir / "asset.png", ns=(original_time, original_time))
    link = tmp_path / "current"

    def point_to(directory):
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(directory), str(link))
        else:
            link.symlink_to(directory, target_is_directory=True)

    try:
        point_to(first_dir)
    except (OSError, AttributeError) as exc:
        pytest.skip(f"Directory links unavailable: {exc}")
    service = VisionService()
    try:
        np.testing.assert_array_equal(service.load_template_image(link / "asset.png"), first)
        link.rmdir() if os.name == "nt" else link.unlink()
        point_to(second_dir)
        np.testing.assert_array_equal(service.load_template_image(link / "asset.png"), second)
    finally:
        if link.exists():
            link.rmdir() if os.name == "nt" else link.unlink()


def test_decode_failure_not_cached_and_retry_works(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not png")
    service = VisionService()
    for _ in range(2):
        with pytest.raises(FileNotFoundError):
            service.load_template_image(path)
    assert not service._template_image_cache._entries
    image = np.zeros((5, 7, 3), dtype=np.uint8)
    write_image(path, image)
    np.testing.assert_array_equal(service.load_template_image(path), image)


def test_preparation_failure_not_published_and_retry_works(monkeypatch, pixels):
    source, template, path = pixels
    service = VisionService()
    original = service._prepare_image
    attempts = []

    def prepare(image, **kwargs):
        if image.shape == template.shape:
            attempts.append(image)
            if len(attempts) == 1:
                raise ValueError("temporary profile failure")
        return original(image, **kwargs)

    monkeypatch.setattr(service, "_prepare_image", prepare)
    assert service.find_template(source, str(path)).debug_info["error"] == "temporary profile failure"
    assert not service._template_image_cache._entries
    assert service.find_template(source, str(path)).found
    assert len(attempts) == 2


def test_cache_pixels_cannot_be_changed_or_made_writable(pixels):
    source, _, path = pixels
    service = VisionService()
    raw = service.load_template_image(path)
    prepared = service._prepare_match_image(str(path), use_grayscale=True, preprocess="edge",
                                           stage="template_prepare_sum", timing=None)
    for image in (raw, prepared):
        with pytest.raises(ValueError):
            image.setflags(write=True)
        with pytest.raises(ValueError):
            image[:] = 0
    assert service.find_template(source, str(path)).found


def test_concurrent_cold_requests_decode_once(monkeypatch, pixels):
    source, _, path = pixels
    service = VisionService()
    calls = track_loads(monkeypatch, service)
    barrier = threading.Barrier(6, timeout=10)

    def match(_):
        barrier.wait()
        return service.find_template(source, str(path))

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(match, range(6)))
    assert all(result.found for result in results)
    assert calls == [str(path)]


def test_batch_shared_mask_decoded_once(monkeypatch, pixels):
    source, template, path = pixels
    mask = path.parent / "mask.png"
    write_image(mask, np.full(template.shape[:2], 255, dtype=np.uint8))
    service = VisionService()
    calls = track_loads(monkeypatch, service)
    for _ in range(2):
        results = service.find_templates_batch(source, [str(path)] * 5,
                                              mask_images=[str(mask)] * 5,
                                              match_method=cv2.TM_CCORR_NORMED)
        assert all(result.found for result in results)
    assert calls.count(str(path)) == calls.count(str(mask)) == 1


def test_cache_lru_and_byte_limits(pixels):
    _, _, path = pixels
    service = VisionService()
    service._template_image_cache = TemplateImageCache(max_assets=1, max_bytes=1024 * 1024)
    first = service.load_template_image(path)
    other = path.parent / "second.png"
    write_image(other, np.zeros((5, 8, 3), dtype=np.uint8))
    service.load_template_image(other)
    assert len(service._template_image_cache._entries) == 1
    assert service.load_template_image(path) is not first
    service._template_image_cache = TemplateImageCache(max_bytes=1)
    service.load_template_image(path)
    assert not service._template_image_cache._entries
    assert service._template_image_cache._bytes == 0


@pytest.mark.parametrize("kind", ["rgb", "gray", "alpha"])
def test_inspect_matches_old_debug_pixels_without_second_decode(monkeypatch, pixels, kind):
    source, template, path = pixels
    bgr = cv2.cvtColor(template, cv2.COLOR_RGB2BGR)
    if kind == "gray":
        bgr = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    elif kind == "alpha":
        bgr = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        bgr[:, :, 3] = np.arange(bgr.shape[1], dtype=np.uint8)
    write_image(path, bgr)
    # Windows cv2.imread cannot open this Unicode filename; compare the same
    # IMREAD_COLOR pixels through the Unicode-safe decoder instead.
    expected = cv2.imdecode(np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
    service = VisionService()
    calls = track_loads(monkeypatch, service)
    monkeypatch.setattr(vision_actions, "resolve_template_path", lambda engine, vision, ref: str(path))
    engine = SimpleNamespace(root_context=SimpleNamespace(data={"initial": {"__is_inspect_mode__": True}}))
    app = SimpleNamespace(capture=lambda **kwargs: SimpleNamespace(success=True, image=source))
    for _ in range(2):
        result = vision_actions.find_image(app, service, engine, str(path))
        np.testing.assert_array_equal(result.debug_info["template_image"], expected)
        result.debug_info["template_image"][:] = 0
    assert calls == [str(path)]


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_inspect_keeps_source_and_params_when_preview_cannot_load(monkeypatch, pixels, kind):
    source, _, path = pixels
    if kind == "missing":
        path.unlink()
    else:
        path.write_bytes(b"invalid png")
    service = VisionService()
    monkeypatch.setattr(vision_actions, "resolve_template_path", lambda engine, vision, ref: str(path))
    engine = SimpleNamespace(root_context=SimpleNamespace(data={"initial": {"__is_inspect_mode__": True}}))
    app = SimpleNamespace(capture=lambda **kwargs: SimpleNamespace(success=True, image=source))
    result = vision_actions.find_image(app, service, engine, str(path))
    assert not result.found
    assert "error" in result.debug_info
    assert result.debug_info["template_image"] is None
    assert result.debug_info["params"]["template"] == str(path)
    np.testing.assert_array_equal(result.debug_info["source_image"], source)
