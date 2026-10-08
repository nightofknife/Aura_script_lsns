"""Pixel-only anonymous grid proposals; no pose, face, source or target proof.

Two bounded methods: edge/dark tile contours, or HSV glyph-centre lattice.
A repeated centre lattice alone cannot establish a complete outer boundary.
The caller supplies only RGB and an ordinary rectangular pixel ROI.
"""
import cv2
import numpy as np


def _order_quad(points):
    q = np.asarray(points, np.float32).reshape(4, 2)
    angles = np.arctan2(q[:, 1]-q[:, 1].mean(), q[:, 0]-q[:, 0].mean())
    q = q[np.argsort(angles)]
    return np.roll(q, -int(np.lexsort((q[:, 0], q[:, 1]))[0]), axis=0)


def _edge_support(q, edges):
    fractions = []
    h, w = edges.shape
    nearby = cv2.dilate(edges, np.ones((5, 5), np.uint8)) > 0
    for a, b in zip(q, np.roll(q, -1, axis=0)):
        n = max(12, int(np.linalg.norm(b-a)))
        pixels = np.rint(a+(b-a)*np.linspace(.03, .97, n)[:, None]).astype(int)
        valid = (pixels[:, 0] >= 0) & (pixels[:, 0] < w) & (pixels[:, 1] >= 0) & (pixels[:, 1] < h)
        fractions.append(float(np.mean(nearby[pixels[valid, 1], pixels[valid, 0]])) if valid.all() else 0.)
    return fractions


def _glyph_mask(rgb):
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    return ((((hsv[..., 1] > 125) & (hsv[..., 2] > 150)) |
             ((hsv[..., 1] < 65) & (hsv[..., 2] > 195))).astype(np.uint8)*255)


def _contour_tiles(rgb, glyph):
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, 50, 120)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    polygons, tiles = [], []
    for contour in contours:
        perimeter = cv2.arcLength(contour, True)
        polygon = cv2.approxPolyDP(contour, .025*perimeter, True)
        if len(polygon) != 4 or not cv2.isContourConvex(polygon):
            continue
        q = _order_quad(polygon)
        area = abs(float(cv2.contourArea(q)))
        if area < 1000:
            continue
        lengths = np.linalg.norm(q-np.roll(q, -1, axis=0), axis=1)
        if min(lengths) < 25 or max(lengths)/min(lengths) > 3:
            continue
        support = _edge_support(q, edges)
        if min(support) < .70:
            continue
        polygons.append(dict(quad=q, area=area, side_support=support))
        if area > 18000:
            continue
        mask = np.zeros(gray.shape, np.uint8)
        cv2.fillConvexPoly(mask, np.int32(q), 1)
        inner = cv2.erode(mask, np.ones((7, 7), np.uint8)) > 0
        pixels = int(inner.sum())
        bright = inner & (glyph > 0)
        glyph_pixels = int(bright.sum())
        dark_fraction = float(np.mean(gray[inner] < 100)) if pixels else 0.
        if dark_fraction < .4 or not 20 <= glyph_pixels <= pixels*.60:
            continue
        centre = q.mean(axis=0)
        if any(np.linalg.norm(centre-item['centre']) < 12 for item in tiles):
            continue
        y, x = np.where(bright)
        tiles.append(dict(centre=centre, quad=q, area=area, side_support=support,
                          glyph_centre=np.array((x.mean(), y.mean())), glyph_pixels=glyph_pixels))
    return tiles, polygons, edges


def _glyph_centres(rgb):
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, sat, val = np.moveaxis(hsv, -1, 0)
    masks = [((hue <= 25) | (hue >= 163)) & (sat > 125) & (val > 150),
             (hue > 25) & (hue < 90) & (sat > 125) & (val > 150),
             (hue >= 90) & (hue < 140) & (sat > 125) & (val > 150),
             (hue >= 140) & (hue < 163) & (sat > 125) & (val > 150),
             (sat < 65) & (val > 195)]
    output = []
    for bucket, mask in enumerate(masks):
        count, _, stats, centres = cv2.connectedComponentsWithStats(mask.astype(np.uint8))
        grouped = []
        for i in range(1, count):
            x, y, w, h, area = map(int, stats[i])
            if not 20 <= area <= 2500 or max(w, h) > 90:
                continue
            # Same-colour separated eye/ring parts can share a glyph centre.
            near = next((g for g in grouped if np.linalg.norm(g['centre']-centres[i]) < 28), None)
            if near is None:
                grouped.append(dict(centre=centres[i].copy(), mass=area, bucket=bucket))
            else:
                near['centre'] = (near['centre']*near['mass']+centres[i]*area)/(near['mass']+area)
                near['mass'] += area
        output.extend(grouped)
    return [dict(centre=g['centre'], quad=None, area=0, side_support=[],
                 glyph_centre=g['centre'], glyph_pixels=g['mass']) for g in output]


def _lattices(items, max_candidates):
    if not 9 <= len(items) <= 128:
        return [], 0
    points = np.asarray([item['centre'] for item in items])
    coordinates = np.array([(c, r) for r in range(3) for c in range(3)], np.float32)
    found = {}
    for origin, point in enumerate(points):
        differences = points-point
        lengths = np.linalg.norm(differences, axis=1)
        neighbours = [i for i in np.argsort(lengths) if 40 <= lengths[i] <= 180][:8]
        for a in neighbours:
            for b in neighbours:
                if a == b:
                    continue
                u, v = differences[a], differences[b]
                sine = abs(float(u[0]*v[1]-u[1]*v[0]))/(lengths[a]*lengths[b])
                if sine < .55:
                    continue
                expected = point+coordinates[:, 0, None]*u+coordinates[:, 1, None]*v
                distances = np.linalg.norm(expected[:, None]-points[None], axis=2)
                ids = np.argmin(distances, axis=1)
                if len(set(ids)) != 9 or max(distances[np.arange(9), ids]) > 16:
                    continue
                homography, _ = cv2.findHomography(coordinates, points[ids].astype(np.float32), 0)
                if homography is None or not np.isfinite(homography).all():
                    continue
                projected = cv2.perspectiveTransform(coordinates[None], homography)[0]
                errors = np.linalg.norm(projected-points[ids], axis=1)
                if max(errors) > 8:
                    continue
                key = tuple(sorted(map(int, ids)))
                candidate = dict(indices=list(map(int, ids)), max_centre_residual_px=float(max(errors)),
                    homography=homography, points=points[ids])
                if key not in found or candidate['max_centre_residual_px'] < found[key]['max_centre_residual_px']:
                    found[key] = candidate
    return (sorted(found.values(), key=lambda c:c['max_centre_residual_px'])[:max_candidates], len(found))


def propose_anonymous_grid(rgb, roi=(300, 84, 950, 615), *, method='dark_tiles', max_candidates=12):
    """Return bounded pixel proposals, ambiguity and measured boundary reasons.

    ROI is (left,top,right,bottom), not a face quad. No R/T input is accepted.
    Even unique complete geometry has face/body/source/targets_bound=False.
    Outer support requires an actual enclosing contour, not extrapolated H.
    """
    report = dict(status='rejected', method=method,
                  roi=list(roi) if isinstance(roi, (list, tuple)) else None,
                  candidates=[], reasons=[],
                  face_bound=False, body_bound=False, source_bound=False,
                  targets_ready=False, input_ready=False)
    if (not isinstance(rgb, np.ndarray) or rgb.ndim != 3 or rgb.shape[2] != 3 or
            rgb.dtype != np.uint8 or method not in ('dark_tiles', 'glyph_centres') or
            type(max_candidates) is not int or not 1 <= max_candidates <= 20):
        report['reasons'] = ['invalid_input']
        return report
    if not isinstance(roi, (list, tuple)) or len(roi) != 4 or any(type(v) is not int for v in roi):
        report['reasons'] = ['invalid_roi']
        return report
    left, top, right, bottom = roi
    if not 0 <= left < right <= rgb.shape[1] or not 0 <= top < bottom <= rgb.shape[0]:
        report['reasons'] = ['invalid_roi']
        return report
    image = rgb[top:bottom, left:right].copy()
    glyph = _glyph_mask(image)
    if method == 'dark_tiles':
        items, polygons, edges = _contour_tiles(image, glyph)
    else:
        items, polygons, edges = _glyph_centres(image), [], None
    report.update(pixel_observations=len(items), contour_boundaries=len(polygons))
    offset = np.array((left, top))
    report['observations'] = [dict(centre=(item['centre']+offset).tolist(),
        quad=(item['quad']+offset).tolist() if item['quad'] is not None else None,
        glyph_centre=(item['glyph_centre']+offset).tolist(),
        glyph_pixels=int(item['glyph_pixels']), side_support=item['side_support']) for item in items]
    if len(items) > 128:
        report['reasons'] = ['too_many_pixel_observations']
        return report
    lattices, found_count = _lattices(items, max_candidates)
    report['candidate_search_truncated'] = found_count > max_candidates
    report['lattice_sets_found'] = found_count
    for lattice in lattices:
        selected = [items[i] for i in lattice['indices']]
        centres = lattice['points']
        boundary = None
        reasons = []
        if method == 'dark_tiles':
            all_corners = np.concatenate([item['quad'] for item in selected])
            hull_area = float(cv2.contourArea(cv2.convexHull(all_corners.astype(np.float32))))
            for polygon in sorted(polygons, key=lambda p:p['area']):
                q = polygon['quad']
                if not hull_area*.95 <= polygon['area'] <= hull_area*1.35:
                    continue
                if not all(cv2.pointPolygonTest(q, tuple(map(float, p)), True) >= -2 for p in all_corners):
                    continue
                # A clipped ROI edge is not evidence of the face boundary.
                if (q[:, 0] <= 2).any() or (q[:, 1] <= 2).any() or (q[:, 0] >= image.shape[1]-3).any() or (q[:, 1] >= image.shape[0]-3).any():
                    continue
                boundary = polygon
                break
            if boundary is None:
                reasons.append('complete_outer_boundary_unobserved')
        else:
            reasons.extend(['tile_boundaries_unobserved', 'complete_outer_boundary_unobserved'])
        # Do not turn a subgrid of a larger repeated lattice into a complete3x3.
        ring = np.array([(c, r) for r in range(-1, 4) for c in range(-1, 4)
                         if c in (-1, 3) or r in (-1, 3)], np.float32)
        extension = cv2.perspectiveTransform(ring[None], lattice['homography'])[0]
        others = [item['centre'] for i, item in enumerate(items) if i not in lattice['indices']]
        if others and (np.linalg.norm(extension[:, None]-np.asarray(others)[None], axis=2) < 16).any():
            reasons.append('repeated_grid_continues_outside_3x3')
        report['candidates'].append(dict(anonymous=True,
            centres=(centres+offset).tolist(),
            glyph_centres=[(item['glyph_centre']+offset).tolist() for item in selected],
            quads=[(item['quad']+offset).tolist() for item in selected] if method == 'dark_tiles' else None,
            tile_border_side_support=[item['side_support'] for item in selected],
            max_centre_residual_px=lattice['max_centre_residual_px'],
            complete_outer_boundary_support=dict(complete=boundary is not None,
                quad=(boundary['quad']+offset).tolist() if boundary else None,
                measured_side_support=boundary['side_support'] if boundary else []),
            complete_anonymous_geometry=not reasons, reasons=reasons,
            orientation_bound=False, shifted_or_flipped_identity_not_resolved=True))
    valid = [c for c in report['candidates'] if c['complete_anonymous_geometry']]
    report['ambiguous'] = len(valid) > 1
    if valid and report['candidate_search_truncated']:
        report['status'] = 'ambiguous'
        report['ambiguous'] = True
        report['reasons'] = ['candidate_budget_hides_other_hypotheses']
    elif len(valid) == 1:
        report['status'] = 'anonymous_geometry_candidate'
    elif len(valid) > 1:
        report['status'] = 'ambiguous'
        report['reasons'] = ['multiple_complete_grid_candidates']
    elif lattices:
        report['status'] = 'partial_candidates'
        report['reasons'] = ['no_complete_measured_grid_boundary']
    else:
        report['reasons'] = ['nine_observed_cells_missing_or_inconsistent']
    return report
