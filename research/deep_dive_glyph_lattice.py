"""Bounded point-only 3x3 glyph-center hypotheses, never measured tile grids.

Missing glyphs are predicted only. D4 orientation, unknown physical sheets,
outer tile boundaries and body identity cannot be established by these points.
No R/T, expected cell, face position, quad or recognition is consumed.
"""
import itertools
import time

import cv2
import numpy as np


COORDS = np.array([(c, r) for r in range(3) for c in range(3)], float)
RING = np.array([(c, r) for r in range(-1, 4) for c in range(-1, 4)
                 if c in (-1, 3) or r in (-1, 3)], float)


def _project(coordinates, homography):
    q = np.column_stack((coordinates, np.ones(len(coordinates)))) @ homography.T
    if (not np.isfinite(q).all() or np.any(np.abs(q[:, 2]) < 1e-8)
            or np.any(q[:, 2]*q[0, 2] <= 0)):
        return None
    return q[:, :2]/q[:, 2, None]


def _match(predicted, points, threshold):
    distances = np.linalg.norm(predicted[:, None]-points[None], axis=2)
    matches = np.full(9, -1, int)
    used = set()
    for flat in np.argsort(distances, axis=None):
        site, observation = np.unravel_index(flat, distances.shape)
        if distances[site, observation] > threshold:
            break
        if matches[site] < 0 and observation not in used:
            matches[site] = observation
            used.add(observation)
    return matches


def _wide(matches, minimum):
    selected = np.flatnonzero(matches >= 0)
    return (len(selected) >= minimum and len(set(selected//3)) == 3
            and len(set(selected % 3)) == 3)


def _aliases(matches, predicted):
    grid, pixels = matches.reshape(3, 3), predicted.reshape(3, 3, 2)
    result = []
    for reflected in (False, True):
        for quarters in range(4):
            ids = np.rot90(np.fliplr(grid) if reflected else grid, quarters)
            positions = np.rot90(np.fliplr(pixels) if reflected else pixels, quarters)
            result.append(dict(reflected=reflected, rotation_quarters=quarters,
                observation_indices=ids.reshape(-1).tolist(),
                predicted_centres=positions.reshape(-1, 2).tolist(), identity_proven=False))
    return result


def propose_glyph_lattices(points, labels=None, *, min_support=7,
                           seed_match_px=16., max_residual_px=8.,
                           spacing_px=(40., 180.), max_candidates=12,
                           max_seed_tests=20000, max_seconds=5.):
    """Enumerate affine seeds, then refine H from >=7 distinct actual centers.

    Labels are optional provenance only: repeated classes do not select or
    reject geometry. All three rows/columns need observed support. A complete
    center lattice still provides no tile boundaries or physical face proof.
    Search/candidate truncation is reported as unresolved ambiguity.
    """
    report = dict(status='rejected', reason='invalid_input', candidates=[],
        proposal_only=True, tile_boundaries_measured=False, outer_boundary_measured=False,
        face_bound=False, body_bound=False, source_bound=False, bank_ready=False,
        targets_ready=False, input_ready=False, physical_sheet_proven=False,
        orientation_bound=False, ambiguous=True, candidate_search_truncated=False)
    started = time.perf_counter()
    try:
        pixels = np.asarray(points, float)
        spacing = np.asarray(spacing_px, float)
        numeric = np.asarray((seed_match_px, max_residual_px, max_seconds), float)
        if (pixels.ndim != 2 or pixels.shape[1] != 2 or not np.isfinite(pixels).all()
                or len(pixels) > 128 or spacing.shape != (2,) or not np.isfinite(spacing).all()
                or not np.isfinite(numeric).all()
                or type(min_support) is not int or not 7 <= min_support <= 9
                or type(max_candidates) is not int or not 1 <= max_candidates <= 64
                or type(max_seed_tests) is not int or not 1 <= max_seed_tests <= 100000
                or not 0 < max_seconds <= 20. or not 0 < max_residual_px <= seed_match_px
                or not 0 < spacing[0] <= spacing[1] or seed_match_px >= spacing[0]/2.
                or labels is not None and len(labels) != len(pixels)):
            return report
    except (ValueError, TypeError):
        return report
    report['pixel_observations'] = len(pixels)
    if len(pixels) < min_support:
        report['reason'] = 'fewer_than_seven_actual_glyph_centres'
        return report
    pair_distance = np.linalg.norm(pixels[:, None]-pixels[None], axis=2)
    # Duplicate centers cannot be counted as different missing glyphs. Caller
    # must resolve conflicting upstream proposals, rather than merge labels.
    if np.any((pair_distance+np.eye(len(pixels))*1e9) < 3.):
        report['reason'] = 'near_duplicate_actual_centres'
        return report
    found, tested, refined, truncated = {}, 0, 0, False
    for origin, point in enumerate(pixels):
        lengths = pair_distance[origin]
        neighbors = [int(i) for i in np.argsort(lengths)
                     if spacing[0] <= lengths[i] <= spacing[1]][:8]
        for a, b in itertools.combinations(neighbors, 2):
            u, v = pixels[a]-point, pixels[b]-point
            sine = abs(float(u[0]*v[1]-u[1]*v[0]))/(lengths[a]*lengths[b])
            if sine < .45:
                continue
            for origin_cell in COORDS:
                if tested >= max_seed_tests or time.perf_counter()-started >= max_seconds:
                    truncated = True
                    break
                tested += 1
                expected = point+(COORDS-origin_cell) @ np.vstack((u, v))
                matches = _match(expected, pixels, seed_match_px)
                if not _wide(matches, min_support):
                    continue
                selected = np.flatnonzero(matches >= 0)
                homography, _ = cv2.findHomography(COORDS[selected], pixels[matches[selected]], 0)
                refined += 1
                if homography is None:
                    continue
                predicted = _project(COORDS, homography)
                if predicted is None:
                    continue
                matches = _match(predicted, pixels, max_residual_px)
                if not _wide(matches, min_support):
                    continue
                selected = np.flatnonzero(matches >= 0)
                # Refit after actual observation assignment changes; no missing
                # predicted site is ever added to the fitting observations.
                homography, _ = cv2.findHomography(COORDS[selected], pixels[matches[selected]], 0)
                if homography is None:
                    continue
                predicted = _project(COORDS, homography)
                if predicted is None:
                    continue
                residuals = np.linalg.norm(predicted[selected]-pixels[matches[selected]], axis=1)
                if np.max(residuals) > max_residual_px:
                    continue
                steps = np.concatenate((np.diff(predicted.reshape(3, 3, 2), axis=0).reshape(-1, 2),
                                        np.diff(predicted.reshape(3, 3, 2), axis=1).reshape(-1, 2)))
                if np.min(np.linalg.norm(steps, axis=1)) < spacing[0]*.7:
                    continue
                aliases = _aliases(matches, predicted)
                key = min(tuple(alias['observation_indices']) for alias in aliases)
                candidate = dict(homography=homography.tolist(), observed_support=len(selected),
                    homography_role='glyph_centre_lattice_not_tile_border_or_body_pose',
                    missing_sites=int(9-len(selected)), centre_residuals_px=residuals.tolist(),
                    max_centre_residual_px=float(np.max(residuals)),
                    median_centre_residual_px=float(np.median(residuals)),
                    observed_rows=sorted(set((selected//3).tolist())),
                    observed_columns=sorted(set((selected % 3).tolist())),
                    orientation_aliases=aliases, orientation_alias_count=8,
                    shifted_or_flipped_identity_not_resolved=True,
                    physical_sheet_proven=False, bank_ready=False, targets_ready=False)
                candidate['sites'] = [dict(row=i//3, col=i % 3,
                    observation_index=int(index) if index >= 0 else None,
                    observed_centre=pixels[index].tolist() if index >= 0 else None,
                    predicted_centre=predicted[i].tolist(),
                    predicted_only=bool(index < 0),
                    site_meaning=('no_matched_accepted_centre_not_empty_tile_evidence' if index < 0
                                  else 'matched_actual_glyph_centre_not_measured_tile_centre'),
                    label=labels[index] if labels is not None and index >= 0 else None)
                    for i, index in enumerate(matches)]
                extension = _project(RING, homography)
                external = [i for i in range(len(pixels)) if i not in matches]
                extension_hits = []
                if extension is not None and external:
                    distances = np.linalg.norm(extension[:, None]-pixels[external][None], axis=2)
                    for ring_id, external_id in zip(*np.where(distances <= max_residual_px)):
                        extension_hits.append(dict(observation_index=external[external_id],
                            lattice_coordinate=RING[ring_id].tolist(),
                            residual_px=float(distances[ring_id, external_id]), observed=True))
                candidate['outside_extension_support'] = extension_hits
                candidate['repeated_grid_continues_outside_3x3'] = bool(extension_hits)
                candidate['unassigned_observation_indices'] = external
                candidate['predicted_only_nearby_unassigned'] = [dict(
                    site_index=int(site), observation_index=int(observation),
                    distance_px=float(np.linalg.norm(predicted[site]-pixels[observation])))
                    for site in np.flatnonzero(matches < 0) for observation in external
                    if np.linalg.norm(predicted[site]-pixels[observation]) <= seed_match_px]
                if key not in found or candidate['max_centre_residual_px'] < found[key]['max_centre_residual_px']:
                    found[key] = candidate
            if truncated:
                break
        if truncated:
            break
    candidates = sorted(found.values(), key=lambda value:(-value['observed_support'], value['max_centre_residual_px']))
    output_truncated = len(candidates) > max_candidates
    report.update(candidates=candidates[:max_candidates], seed_tests=tested,
        homography_refinements=refined, geometric_hypotheses_found=len(candidates),
        candidate_search_truncated=bool(truncated or output_truncated),
        seed_search_truncated=truncated, output_candidates_truncated=output_truncated,
        spatial_ambiguity=len(candidates) > 1 or truncated or output_truncated,
        elapsed_sec=time.perf_counter()-started,
        status='proposal_only' if candidates else 'rejected',
        reason=('candidate_budget_hides_other_hypotheses' if truncated or output_truncated else
                'glyph_lattice_requires_actual_tile_boundary_and_identity' if candidates else
                'no_seven_point_three_row_three_column_lattice'),
        missing_observations_never_used_to_fit=True)
    return report
