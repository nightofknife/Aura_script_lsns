"""Native-font template reading for fixed 1280x720 HUD numeric pairs.

The reader does not know the expected quota or inspiration count. It accepts
only a complete, unambiguous sequence of numeric glyphs and one slash, so an
extra move/rotation allowance remains visible to the caller's rules gate.
"""
from __future__ import annotations

from functools import lru_cache
import json
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[2] / 'templates/deep_dive_planned_run/hud_numbers/pairs'
REGIONS = {
    'moves': (1165, 386, 1250, 432),
    'rotations': (1165, 457, 1250, 505),
    # The numeric suffix is right-aligned; the Chinese objective text is not a glyph source.
    'inspirations': (1215, 257, 1271, 287),
}
FIELDS = {
    'moves': ('moves_used', 'moves_total'),
    'rotations': ('rotations_used', 'rotations_total'),
    'inspirations': ('collected_count', 'inspiration_total'),
}


def _normalise(mask):
    yy, xx = np.where(mask > 0)
    if len(xx) < 6:
        return None
    glyph = mask[yy.min():yy.max()+1, xx.min():xx.max()+1].astype(np.float32)
    height, width = glyph.shape
    scale = 28 / max(height, width)
    resized = cv2.resize(glyph, (max(1, round(width*scale)), max(1, round(height*scale))),
                         interpolation=cv2.INTER_AREA)
    output = np.zeros((32, 32), np.float32)
    top, left = (32-resized.shape[0])//2, (32-resized.shape[1])//2
    output[top:top+resized.shape[0], left:left+resized.shape[1]] = resized
    return output, width / height, height


@lru_cache(maxsize=1)
def _templates():
    try:
        catalog = json.loads((ROOT/'sources.json').read_text(encoding='utf8'))
    except (OSError, ValueError):
        return ()
    rows = []
    for entry in catalog['entries']:
        alpha = cv2.imread(str(ROOT/entry['file']), cv2.IMREAD_GRAYSCALE)
        if alpha is None:
            continue
        normalised = _normalise((alpha >= 100).astype(np.float32))
        if normalised is not None:
            rows.append((entry['character'], entry['screen_font_size'], entry.get('font'),
                         entry['file'], *normalised))
    return tuple(rows)


def _glyph(mask, font_size):
    normalised = _normalise(mask)
    if normalised is None:
        return None
    observed, ratio, height = normalised
    scores = {}
    sources = {}
    for character, size, font, file_name, reference, reference_ratio, reference_height in _templates():
        if size != font_size or abs(height-reference_height) > 3:
            continue
        # A slash and a narrow 1 can have similar area but different width/height.
        ratio_score = max(0., 1 - abs(ratio-reference_ratio)/max(reference_ratio, .1))
        intersection = float(np.minimum(observed, reference).sum())
        dice = 2 * intersection / max(float(observed.sum()+reference.sum()), 1.)
        score = .90*dice + .10*ratio_score
        if score > scores.get(character, 0.):
            scores[character] = score
            sources[character] = dict(font=font, template=file_name)
    ranked = sorted(scores.items(), key=lambda row: -row[1])
    if len(ranked) < 2:
        return None
    (character, best), (_, runner) = ranked[:2]
    return dict(character=character, confidence=round(best, 4),
                margin=round(best-runner, 4), alternatives=ranked[:3], **sources[character])


def _read_pair(rgb, name):
    x1, y1, x2, y2 = REGIONS[name]
    roi = rgb[y1:y2, x1:x2]
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    cyan = ((hsv[:, :, 0] >= 80) & (hsv[:, :, 0] <= 105) &
            (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 135))
    cyan_fraction = float(cyan[8:36, 8:-8].mean()) if name != 'inspirations' else 0.
    active_cyan = name != 'inspirations' and cyan_fraction >= .55
    yellow_quota = False
    if active_cyan:
        # Active controls reverse the text colour. Only dark glyphs enclosed
        # by this frame's filled cyan surface are eligible; a normal dark
        # board/button background is never treated as black foreground text.
        surface = cv2.morphologyEx(cyan.astype(np.uint8), cv2.MORPH_CLOSE,
                                   np.ones((7, 7), np.uint8))
        foreground = ((hsv[:, :, 2] <= 120) & (surface > 0)).astype(np.uint8)
    else:
        foreground = ((hsv[:, :, 1] <= 80) & (hsv[:, :, 2] >= 145)).astype(np.uint8)
        if name == 'rotations':
            # The native completed rotation control renders its numeric text
            # yellow, as well as a separate circled check to its left. Only
            # the fixed text suffix is a glyph source; the check never supplies
            # either number. White pixels in this suffix remain eligible so a
            # conflicting/extra light glyph cannot silently disappear.
            yellow = ((hsv[:, :, 0] >= 20) & (hsv[:, :, 0] <= 40) &
                      (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 145))
            yellow_quota = bool(np.any(yellow[8:36, 24:]))
            if yellow_quota:
                foreground = (foreground | yellow.astype(np.uint8))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(foreground, 8)
    font_size = 18 if name == 'inspirations' else 22
    components = []
    unexpected_badge_foreground = False
    for index in range(1, count):
        x, y, width, height, area = map(int, stats[index])
        if yellow_quota and x < 24 and 8 <= y <= 22 and y+height <= 36 and height >= 9 and area >= 12:
            # Exclude only the actual prefab's left circle/check components.
            # A shifted/extra digit or a component crossing into the text
            # strip is uncertainty, not a prefix that can be cropped away.
            badge_circle = (0 <= x <= 4 and 9 <= y <= 14 and
                            19 <= width <= 23 and 19 <= height <= 23)
            badge_check = (4 <= x <= 8 and 15 <= y <= 20 and
                           10 <= width <= 15 and 9 <= height <= 12)
            if x+width <= 24 and (badge_circle or badge_check):
                continue
            unexpected_badge_foreground = True
        if not (2 <= width <= 22 and 9 <= height <= 22 and area >= 12):
            continue
        # The quota text is centered vertically within its fixed control.
        if name != 'inspirations' and not (8 <= y <= 22 and y+height <= 36):
            continue
        mask = (labels[y:y+height, x:x+width] == index).astype(np.float32)
        match = _glyph(mask, font_size)
        components.append(dict(box=[x+x1, y+y1, width, height], match=match))
    components.sort(key=lambda row: row['box'][0])
    evidence = dict(status='unknown', region=list(REGIONS[name]), source='native_font_templates',
                    foreground=('dark_on_verified_cyan' if active_cyan else
                                'yellow_rotation_text' if yellow_quota else 'light'),
                    cyan_background_fraction=round(cyan_fraction, 4),
                    fonts=['HarmonyOS_Sans_Medium', 'SourceHanSansCN-Medium'] if font_size == 18
                    else ['HarmonyOS_Sans_Medium'], font_size=font_size,
                    confidence=0., glyphs=components)
    if unexpected_badge_foreground:
        evidence['reason'] = 'unexpected_foreground_in_rotation_badge'
        return None, evidence
    if not 3 <= len(components) <= 7:
        evidence['reason'] = 'incomplete_or_extra_numeric_components'
        return None, evidence
    text = ''
    for component in components:
        match = component['match']
        if match is None or match['confidence'] < .76 or match['margin'] < .045:
            evidence['reason'] = 'ambiguous_numeric_glyph'
            return None, evidence
        text += match['character']
    if text.count('/') != 1:
        evidence['reason'] = 'numeric_separator_not_unique'
        return None, evidence
    if text.startswith('(') and text.endswith(')'):
        text = text[1:-1]
    left, right = text.split('/')
    if not (left.isdecimal() and right.isdecimal() and len(left) <= 2 and len(right) <= 2):
        evidence['reason'] = 'invalid_numeric_pair_shape'
        return None, evidence
    # Brackets are ignored as decoration, but all accepted numeric characters
    # must belong to the same text row with normal inter-character spacing.
    centres = [row['box'][1]+row['box'][3]/2 for row in components]
    gaps = [b['box'][0]-(a['box'][0]+a['box'][2]) for a, b in zip(components, components[1:])]
    if max(centres)-min(centres) > 4 or any(gap < -1 or gap > 9 for gap in gaps):
        evidence['reason'] = 'numeric_components_not_one_row'
        return None, evidence
    used, total = int(left), int(right)
    if total < 1 or used > total:
        evidence['reason'] = 'numeric_pair_out_of_range'
        return None, evidence
    evidence.update(status='recognized', text=text,
                    confidence=min(row['match']['confidence'] for row in components))
    return (used, total), evidence


def read_numeric_pairs(rgb):
    """Return six int-or-None fields and per-pair visual evidence; never OCR."""
    result = {field: None for fields in FIELDS.values() for field in fields}
    result.update(evidence={}, confidence={}, schema='resonance_pc.deep_dive_hud_pairs.v1')
    if not isinstance(rgb, np.ndarray) or rgb.shape != (720, 1280, 3) or rgb.dtype != np.uint8:
        result['status'] = 'invalid_frame'
        return result
    for name, fields in FIELDS.items():
        pair, evidence = _read_pair(rgb, name)
        result['evidence'][name] = evidence
        result['confidence'][name] = evidence.get('confidence', 0.)
        if pair is not None:
            result[fields[0]], result[fields[1]] = pair
    result['status'] = ('complete' if all(result[field] is not None for fields in FIELDS.values()
                                        for field in fields) else 'partial')
    return result
