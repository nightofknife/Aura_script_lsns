"""Real glyph rotations retain the original scores, margins and rejection."""
import cv2
import numpy as np
from plans.resonance_pc.src.actions import _deep_dive_layout_semantics as semantics
from test_deep_dive_eye_matmul_guard import original_scores


def test_batched_eye_scores_preserve_native_rotation_and_translation(monkeypatch):
    root = semantics.Path(semantics.__file__).resolve().parents[2] / 'templates/deep_dive_layout'
    examples = 0
    for path in root.glob('icon_*eye*.png'):
        rgb = cv2.cvtColor(cv2.imread(str(path)), cv2.COLOR_BGR2RGB)
        for angle in range(0, 360, 15):
            transformed = (np.ascontiguousarray(np.rot90(rgb, angle // 90)) if angle % 90 == 0 else
                           cv2.warpAffine(rgb, cv2.getRotationMatrix2D((47.5, 47.5), angle, 1.), (96, 96)))
            # The native right-angle cases also own the public classifier and
            # original-kernel regressions; do not repeat them in other matrices.
            if angle % 90 == 0:
                with monkeypatch.context() as patch:
                    patch.setattr(semantics, '_eye_correlation_scores', original_scores)
                    expected = semantics.classify_icon(transformed)
                public_result = semantics.classify_icon(transformed)
                assert public_result == expected
                expected_icon = {
                    'icon_red_single_eye': 'red_single_eye',
                    'icon_orange_triple_eye': 'orange_triple_eye',
                    'icon_orange_triple_eye_2': 'orange_triple_eye',
                    'icon_orange_triple_eye_3': 'orange_triple_eye',
                }.get(path.stem)
                if expected_icon is not None:
                    assert public_result['icon_id'] == expected_icon
            glyph = semantics._warm_glyph(transformed)
            actual = sorted(semantics._eye_correlation_scores(glyph), reverse=True) if glyph is not None else None
            if angle % 90 == 0:
                for whole in (False, True):
                    normalized = semantics._warm_glyph(transformed, whole=True) if whole else glyph
                    if normalized is None:
                        continue
                    scores = sorted(semantics._eye_correlation_scores(normalized, whole), reverse=True) if whole else actual
                    reference = sorted(original_scores(normalized, whole), reverse=True)
                    np.testing.assert_allclose([score for score, _ in scores],
                                               [score for score, _ in reference], atol=1e-6, rtol=0.)
            if glyph is None:
                continue
            original = sorted([(max(float(cv2.minMaxLoc(cv2.matchTemplate(
                np.pad(glyph, 4), variant, cv2.TM_CCORR_NORMED))[1]) for variant in variants), name)
                for name, variants in semantics._eye_templates().items()], reverse=True)
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
