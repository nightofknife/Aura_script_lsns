"""Real glyph rotations retain the original scores, margins and rejection."""
import cv2
import numpy as np
from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as semantics


def test_batched_eye_scores_preserve_native_rotation_and_translation():
    root = semantics.Path(semantics.__file__).resolve().parents[2] / 'templates/deep_dive_layout'
    examples = 0
    for path in root.glob('icon_*eye*.png'):
        rgb = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)
        for angle in range(0, 360, 15):
            transformed = cv2.warpAffine(rgb, cv2.getRotationMatrix2D((47.5, 47.5), angle, 1.), (96, 96))
            glyph = semantics._warm_glyph(transformed)
            if glyph is None:
                continue
            original = sorted([(max(float(cv2.minMaxLoc(cv2.matchTemplate(
                np.pad(glyph, 4), variant, cv2.TM_CCORR_NORMED))[1]) for variant in variants), name)
                for name, variants in semantics._eye_templates().items()], reverse=True)
            actual = sorted(semantics._eye_correlation_scores(glyph), reverse=True)
            assert [name for _, name in actual] == [name for _, name in original]
            np.testing.assert_allclose([score for score, _ in actual], [score for score, _ in original], atol=1e-6, rtol=0.)
            accepted = original[0][1] if original[0][0] >= .78 and original[0][0] - original[1][0] >= .055 else None
            classified = semantics._classify_eye_shape(transformed)['icon_id']
            if accepted is not None:
                assert classified == accepted
            elif classified is not None:
                whole = semantics._warm_glyph(transformed, whole=True)
                assert whole is not None
                fallback = sorted(semantics._eye_correlation_scores(whole, whole=True), reverse=True)
                assert fallback[0][1] == classified
                assert fallback[0][0] >= .78 and fallback[0][0] - fallback[1][0] >= .055
            examples += 1
    assert examples >= 90


def test_zero_shape_has_no_template_evidence():
    assert all(score == 0. for score, _ in semantics._eye_correlation_scores(np.zeros((64,64), np.float32)))
