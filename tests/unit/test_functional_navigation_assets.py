"""Byte/mode contracts for navigation and passenger controls without catalogs.

The migration changes resource ownership only; visual states remain independent.
Catalog-owned city, departure, warehouse and trailer assets have separate tests.
"""
from hashlib import sha256
from pathlib import Path

from PIL import Image
import pytest

PLAN = Path(__file__).resolve().parents[2] / "plans/resonance_pc"

# Snapshots taken before the functional directory migration.
CONTROLS = [
    ('templates/navigation/global/back.png', '98be348c46581cddfdc0575125d2b38c99ceb2958b4521837a996223f9c3e443', 'RGBA', (134, 46)),
    ('templates/navigation/global/city_main.png', 'c7dff2f7570d3ccb9dcb052b2170d9edcdc61b60c1c3dada1074889e40d365f2', 'RGBA', (82, 46)),
    ('templates/navigation/global/train_maintenance.png', '8d801e308613cf9b9deeeb8f7f8f473379f59768264b53f1c9d27d2a8846d54d', 'RGBA', (34, 33)),
    ('templates/navigation/travel/enter_station.png', '92a9c6069d8e0a9695b78412831f2bd640afe835e0b68537cea27b1eca37f45c', 'RGB', (203, 43)),
    ('templates/navigation/travel/go_destination.png', '0a5d8a1092d97f8dfcfbb70e368e9e9a29a5fd3cb7d6f8480d1fc1368f00d3b8', 'RGB', (269, 61)),
    ('templates/navigation/travel/horn.png', 'affa2ec997b35ddb51f1d8c70a4f1e647a3385a073dab0cb40d4bbc52b10699a', 'RGBA', (44, 44)),
    ('templates/navigation/city/visit_entry.png', '121af10b632c6253d787f012caf3a938345f3010e2895b317f95ad85a8f36b51', 'RGB', (35, 29)),
    ('templates/navigation/warehouse/entry.png', '7da7fed00c56263a0a7c24065c94df9c6102faee9ee5498f861e12c3d224bd0a', 'RGBA', (60, 60)),
    ('templates/passenger/management/entry.png', 'bb32e4c1ff805ba12565a33ab240f2e1f78a832e33582457ec1c2902cd53d98b', 'RGB', (56, 71)),
    ('templates/passenger/management/score_marker.png', '52ca8535a8b133190a3da0d51c225a40c07dbd4a83d3eb43d5bdc6a9dcb398a4', 'RGBA', (255, 105)),
    ('templates/passenger/recruitment/entry.png', '989e0b728309df35b6894e11e4683d66e7b5f6df516b072de9a70ace745d76ca', 'RGBA', (165, 195)),
    ('templates/passenger/recruitment/flyer_mode.png', '719154af44db86c7d170f1c69f43882070540796d85898620553c39b3fa2efa9', 'RGBA', (390, 75)),
    ('templates/passenger/destination/marker.png', 'f3680f332a7e0d64f11126890f52bb83dd8df83522f4be45a1359a48687c0773', 'RGBA', (390, 55)),
    ('templates/passenger/destination/next.png', '8447fd270c5d14e42a0a031b676384c25f0121afba9f116e4021d3dd62a0d414', 'RGBA', (220, 55)),
    ('templates/passenger/dispatch/marker.png', '06897ddaa2e365beee8da6424fd26b9e1464adbd0c33d4c5db253a4e74a67c63', 'RGBA', (350, 60)),
    ('templates/passenger/dispatch/location_marker.png', 'd625cd31fc1aeb1b591cfe8a373684b4fc466c98491ae8913495c9324636c4cf', 'RGBA', (22, 30)),
    ('templates/passenger/dispatch/lock.png', 'dde8f05e0e53905cfa5dde9b8c653a360affd1ab5ab8d458327eb028b1e534e6', 'RGBA', (38, 46)),
    ('templates/passenger/dispatch/confirm.png', '3004e3bbe5e5205a404b1b1c0a0ff4ec5cc18fdd7d65df5c74c0baec2bdfd669', 'RGBA', (215, 60)),
    ('templates/passenger/amount/maximum.png', 'b28b3ba83627cb617b549d5e05d759c0a8f3335f7d4eab71d5567a8e9d9a6e6b', 'RGBA', (72, 65)),
    ('templates/passenger/amount/confirm.png', '7709c0e1679e2081afdb0b3af7f57ba185aa07ef29f88f7e646492ff6a76f6b0', 'RGBA', (230, 62)),
    ('templates/passenger/recruitment/success.png', '130b722c920065cdc7339e9462efdfcfd0be7f00e0f24a488cfbaad38d6356a5', 'RGBA', (430, 100)),
    ('templates/passenger/settlement/revenue.png', '7d493cc75f841106ddb49a281be97a958ffec25f70d03402e00cf34983d9e619', 'RGBA', (430, 105)),
]


@pytest.mark.parametrize(("relative", "expected_sha", "mode", "size"), CONTROLS)
def test_functional_controls_preserve_original_pixels_and_image_mode(relative, expected_sha, mode, size):
    path = PLAN / relative
    assert path.resolve().is_relative_to((PLAN / "templates").resolve())
    assert sha256(path.read_bytes()).hexdigest() == expected_sha
    with Image.open(path) as image:
        assert image.mode == mode
        assert image.size == size


def test_passenger_state_bank_has_no_orphans_or_missing_controls():
    expected = {relative for relative, _, _, _ in CONTROLS if relative.startswith("templates/passenger/")}
    assert len(expected) == 14
    assert {path.relative_to(PLAN).as_posix() for path in (PLAN / "templates/passenger").rglob("*.png")} == expected
