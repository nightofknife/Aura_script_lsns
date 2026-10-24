"""Independent geometry evidence: digit width, page envelopes and stable pairs."""
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

from plans.aura_base.src.services.observation_service import Frame
from plans.resonance_pc.src.actions._fatigue_observation import (
    FatigueConfirmation, read_fatigue_pair, split_fatigue_numbers,
)


GLYPHS = Path('plans/resonance_pc/development/template_sources/navigation/profile/status_digits')


def render_pair(current, maximum, width, height, *, align='right'):
    groups = []
    for text in (str(current), '/', str(maximum)):
        glyphs = []
        for ch in text:
            pixels = cv2.imread(str(GLYPHS / ('slash.png' if ch == '/' else ch + '.png')), 0)
            ys, xs = np.where(pixels > 170)
            glyphs.append(pixels[ys.min():ys.max()+1, xs.min():xs.max()+1])
        group = np.zeros((max(g.shape[0] for g in glyphs), sum(g.shape[1]+2 for g in glyphs)-2), np.uint8)
        x = 0
        for glyph in glyphs:
            group[:glyph.shape[0], x:x+glyph.shape[1]] = glyph
            x += glyph.shape[1] + 2
        groups.append(group)
    text_width = sum(g.shape[1] for g in groups) + 4
    assert text_width < width - 2
    image = np.full((height, width, 3), 30, np.uint8)
    x = width - text_width - 2 if align == 'right' else (width-text_width)//2
    y = 5
    bounds = []
    for group in groups:
        image[y:y+group.shape[0], x:x+group.shape[1]] = group[:, :, None]
        bounds.append((x, y, group.shape[1], group.shape[0]))
        x += group.shape[1] + 2
    return image, bounds


@pytest.mark.parametrize('current', [0, 77, 600, 1000, 1111, 7777])
@pytest.mark.parametrize('maximum', [9, 99, 999, 1000])
@pytest.mark.parametrize('width,height', [(98,25), (95,24), (132,40)])
@pytest.mark.parametrize('align', ['right', 'center'])
def test_all_digit_counts_preserve_every_digit_in_each_page_envelope(current, maximum, width, height, align):
    image, bounds = render_pair(current, maximum, width, height, align=align)
    crops, reason = split_fatigue_numbers(image)
    assert reason == 'recognized'
    for crop, (x, y, w, h) in zip(crops, (bounds[0], bounds[2])):
        # Pixel equality proves the first and last glyphs survive the split.
        expected = image[y:y+h, x:x+w]
        location = cv2.minMaxLoc(cv2.matchTemplate(crop, expected, cv2.TM_SQDIFF))[2]
        cx, cy = location
        np.testing.assert_array_equal(crop[cy:cy+h, cx:cx+w], expected)


@pytest.mark.parametrize('path', [
    'tests/fixtures/profile_status_digits/fatigue_45_864.png',
    'tests/fixtures/profile_status_digits/fatigue_58_864.png',
    'tests/fixtures/fatigue_observation/buy.png',
    'tests/fixtures/fatigue_observation/sell.png',
    'tests/fixtures/bento_consumption/work_usable.png',
    'tests/fixtures/bento_consumption/love_usable.png',
])
def test_real_page_pixels_are_split_before_ocr_without_reading_a_separator(path):
    image = cv2.imread(path)
    if image.shape[:2] == (720,1280):
        image = image[20:60,1139:1271]
    received = []

    def recognize_all(*, source_image):
        received.append(source_image)
        return NS(success=True, results=[NS(text='77' if len(received)==1 else '872', confidence=.99)])

    roi = (10,20,image.shape[1],image.shape[0])
    frame = Frame.from_image(image, roi=roi)
    assert read_fatigue_pair(frame, roi, NS(recognize_all=recognize_all)) == ((77,872), 'recognized')
    assert len(received) == 2


def test_edge_clipping_is_unknown_instead_of_accepting_a_truncated_number():
    image, bounds = render_pair(1000,1000,98,25)
    x = bounds[0][0]
    assert split_fatigue_numbers(image[:, x+1:])[1] == 'fatigue_digits_clipped'


def test_two_separators_are_ambiguous_instead_of_guessing_a_split():
    image, _ = render_pair('0/9',9,132,40)
    assert split_fatigue_numbers(image)[0] is None


def test_reused_frame_and_intervening_failure_cannot_confirm_fatigue():
    stable = FatigueConfirmation()
    assert not stable.feed((77,872), 'a')
    assert not stable.feed((77,872), 'a')
    assert not stable.feed(None, 'b')
    assert not stable.feed((77,872), 'c')
    assert stable.feed((77,872), 'd')
    assert not stable.feed((77,900), 'e')
    assert stable.feed((77,900), 'f')
