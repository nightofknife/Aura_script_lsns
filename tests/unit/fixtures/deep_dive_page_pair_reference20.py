import math
import numpy as np
from plans.resonance_pc.src.actions._deep_dive_scan_policy import _matrix, _unit

def select_page_pair_reference20(self, observed, rows, axes):
    face = self._page_face
    indices = np.array([i for i, k in enumerate(self._keys) if k[0] == face])
    turns = np.radians((-150., -120., -90., -60., -40., -25., -15.,
                        0., 15., 25., 40., 60., 90., 120., 150.))
    second_angles = np.radians(np.arange(-150., 151., 6.))
    poses, paths, costs, families, values = [], [], [], [], []
    for first, second in ((0, 1), (1, 0)):
        for i, a in enumerate(turns):
            intermediate = _matrix(_unit(axes[first]) * a) @ observed
            for b in second_angles:
                final = _matrix(_unit(axes[second]) * b) @ intermediate
                poses.append(final)
                paths.append(([intermediate] if abs(a) > .01 else []) + [final])
                costs.append(abs(a) + abs(b))
                families.append((first, i, second))
                values.append(b)
    poses = np.asarray(poses)
    camera = np.einsum('bij,j->bi', poses, self._points[indices[4]]) + self.tvec.reshape(3)
    normals = np.einsum('bij,j->bi', poses, self._normals[indices[4]])
    cosine = -np.sum(camera * normals, axis=1) / np.linalg.norm(camera, axis=1)
    readable = self._readability(poses, rows)
    glyphs = self._readability(poses, rows, anchors=True, known_only=False) > 0
    side_counts = np.stack([glyphs[:, j*9:(j+1)*9].sum(axis=1)
                           for j in range(6) if self._keys[j*9][0] != face], axis=1)
    support = self._planning_anchor_support(poses, rows)
    route_support = self._route_anchor_support(paths, rows, allow_potential=True)
    valid = ((cosine >= .86) & (cosine <= .925) & (side_counts.max(axis=1) >= 2)
             & ((readable[:, indices] > 0).sum(axis=1) >= 6)
             & (support > 0) & (route_support > 0))
    rate = self._observed_motion_rate or math.radians(24.)
    best = None
    for family in set(families):
        choices = [i for i, f in enumerate(families) if f == family and valid[i]]
        for left in choices:
            for right in choices:
                difference = values[right] - values[left]
                if not math.radians(14.) <= abs(difference) <= math.radians(20.):
                    continue
                # Third endpoint, if needed, is one further step along the
                # same second input axis; no return to the first-leg pose.
                alternatives = [i for i in choices if (values[i]-values[right])*difference > 0
                                and math.radians(10.) <= abs(values[i]-values[right]) <= math.radians(20.)]
                alternative = min(alternatives, key=lambda i: abs(values[i]-values[right])) if alternatives else None
                visible = (readable[[left, right]][:, indices] > 0).any(axis=0).sum()
                quality = readable[[left, right]][:, indices].sum()/18.
                upright = max(0., -float(normals[[left, right], 1].mean()))
                near = np.clip(1.-abs(cosine[[left, right]].mean()-.90)/.04, 0., 1.)
                cost = costs[left] + abs(difference)
                utility = (visible/9.+quality+.10*upright+.10*near)
                utility *= math.sqrt(min(support[left], support[right])*route_support[left])
                utility /= .7+cost/max(rate, math.radians(3.))
                if best is None or utility > best[0]:
                    best = (utility, left, right, alternative)
    if best is None:
        return None
    _, left, right, alternative = best
    sequence = [left, right] + ([] if alternative is None else [alternative])
    return [poses[i] @ self._page_basis.T for i in sequence], paths[left]
