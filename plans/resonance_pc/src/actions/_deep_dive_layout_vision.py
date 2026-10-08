"""Experimental RGB cube atlas, with explicit unknowns and source-frame evidence.

The geometric seed was fitted to the user's 2026-09-27 1280x720 recording.
It is only used after a visually verified Reset View. Every new session must
refit visible icon centres before tracking; no board contents are hard-coded.
"""
from __future__ import annotations

import math
import itertools
import time
from collections import Counter
from copy import deepcopy
from functools import lru_cache

import cv2
import numpy as np

from ._deep_dive_layout_semantics import classify_icon, detect_targets


FACES = ("U", "R", "F", "D", "L", "B")
# Right/down surface axes are explicit, including the back and bottom faces.
BASES = {
    "U": ((0, -1, 0), (1, 0, 0), (0, 0, -1)),
    "R": ((1, 0, 0), (0, 0, 1), (0, 1, 0)),
    "F": ((0, 0, -1), (1, 0, 0), (0, 1, 0)),
    "D": ((0, 1, 0), (1, 0, 0), (0, 0, 1)),
    "L": ((-1, 0, 0), (0, 0, -1), (0, 1, 0)),
    "B": ((0, 0, 1), (-1, 0, 0), (0, 1, 0)),
}
DISTANCE = 2.30268665
HALF_TILE = .43
K = np.array(((4320.38916, 0., 640.), (0., 4320.38916, 360.), (0., 0., 1.)))
SEED_R = np.array((.49622186, .78249304, .1948536)).reshape(3, 1)
SEED_T = np.array((.00697532, .42633218, 42.1264626)).reshape(3, 1)


def _angle(a, b):
    return float(np.degrees(np.linalg.norm(cv2.Rodrigues(a @ b.T)[0])))


def _independent_positive_witness(observations, kind, map_revision, required=2):
    """Return actual pairwise-separated source votes, never group-centre poses.

    Groups select where to keep a best image. That image may move towards a
    neighbouring group, so the groups alone do not prove independent views.
    Keep the raw evidence untouched and find a witness using its saved poses.
    """
    candidates = []
    for item in observations:
        proof = item.get('association_evidence') or {}
        if not isinstance(proof, dict):
            continue
        try:
            frame, group = item.get('frame_id'), item.get('group')
            confidence = item.get('confidence')
            if (item.get('occupant') != kind or type(frame) is not int or frame < 0
                    or type(group) is not int or group < 0
                    or isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                    or not math.isfinite(confidence) or not .70 <= confidence <= 1.
                    or type(proof.get('source_frame_id')) is not int
                    or proof.get('source_frame_id') != frame
                    or type(proof.get('source_map_revision')) is not int
                    or proof['source_map_revision'] != map_revision):
                continue
            rotation = np.asarray(proof.get('rotation'), float)
            if (rotation.shape != (3, 3) or not np.isfinite(rotation).all()
                    or not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-5, rtol=0.)
                    or abs(float(np.linalg.det(rotation))-1.) > 1e-5):
                continue
            candidates.append((item, rotation))
        except (TypeError, ValueError):
            continue
    candidates.sort(key=lambda x: (-x[0]['confidence'], x[0]['frame_id'], x[0]['group']))
    if len(candidates) < required:
        return []
    # A clique witnesses pairwise separation. Searching all possible first
    # votes avoids the 0-degree best vote hiding a valid -6/+6-degree pair.
    adjacent = [0] * len(candidates)
    for i, (first, rotation) in enumerate(candidates):
        for j in range(i+1, len(candidates)):
            second, other = candidates[j]
            if (first['frame_id'] != second['frame_id'] and first['group'] != second['group']
                    and _angle(rotation, other) >= 8.):
                if required == 2:
                    return [first, second]
                adjacent[i] |= 1 << j
                adjacent[j] |= 1 << i

    def find(chosen, remaining):
        if len(chosen) == required:
            return chosen
        while remaining.bit_count() >= required-len(chosen):
            bit = remaining & -remaining
            index = bit.bit_length()-1
            remaining ^= bit
            witness = find(chosen+[index], remaining & adjacent[index])
            if witness is not None:
                return witness
        return None

    witness = find([], (1 << len(candidates))-1)
    return [candidates[i][0] for i in witness] if witness is not None else []


def _ui_mask():
    mask = np.zeros((720, 1280), np.uint8)
    mask[82:619, 295:951] = 255
    mask[494:579, 605:677] = 0  # reset UI is screen-fixed, not a surface feature
    return mask


def _point(cell):
    return _grid_point(cell['face'],cell['row'],cell['col'])


@lru_cache(maxsize=54)
def _grid_point(face,row,col):
    n,u,v=(np.asarray(a,float) for a in BASES[face])
    point=n*DISTANCE+u*(col-1)+v*(row-1)
    point.flags.writeable=False
    return point


def _quad(cell):
    _, u, v = (np.asarray(a, float) for a in BASES[cell["face"]])
    centre = _point(cell)
    return np.array([centre + u*x*HALF_TILE + v*y*HALF_TILE
                     for x,y in ((-1,-1),(1,-1),(1,1),(-1,1))])


def _anchor_heights(target):
    if target['kind'] == 'player':
        return {'head': (.20, .85), 'body': (.06, .55),
                'contact': (0., .12)}.get(target.get('anchor_type', 'head'), (.20, .85))
    return {'inspiration': (.05, .65), 'singularity': (.20, 1.)}[target['kind']]


def _multi_face_grid_support(objects,points,minimum=6):
    if len(objects)<minimum:
        return False
    faces=Counter((int(np.argmax(np.abs(point))),int(np.sign(point[np.argmax(np.abs(point))])))
                  for point in objects)
    return bool(sum(count>=2 for count in faces.values())>=2 and
                np.min(np.ptp(points,axis=0))>=80 and
                cv2.contourArea(cv2.convexHull(np.float32(points)))>=7000)


def _neighbors(cell):
    """Geometric surface adjacency only; this does not authorize game moves."""
    normal,u,v=(np.asarray(a,int) for a in BASES[cell['face']])
    result={}
    for name,dr,dc in (('up',-1,0),('down',1,0),('left',0,-1),('right',0,1)):
        r,c=cell['row']+dr,cell['col']+dc
        if 0<=r<3 and 0<=c<3:
            result[name]=dict(face=cell['face'],row=r,col=c);continue
        next_normal=v*dr if dr else u*dc
        face=next(f for f in FACES if np.array_equal(BASES[f][0],next_normal))
        _,next_u,next_v=(np.asarray(a,int) for a in BASES[face])
        edge=normal+(u*(cell['col']-1) if dr else v*(cell['row']-1))
        result[name]=dict(face=face,row=int(edge@next_v)+1,col=int(edge@next_u)+1)
    return result


def _crop(image, quad, size=96):
    matrix = cv2.getPerspectiveTransform(np.float32(quad),
        np.float32(((0,0),(size-1,0),(size-1,size-1),(0,size-1))))
    return cv2.warpPerspective(image, matrix, (size,size))


def _icon_mask(rgb,name):
    hsv=cv2.cvtColor(rgb,cv2.COLOR_RGB2HSV);h,s,v=cv2.split(hsv)
    return {
        'white_diamond':(s<62)&(v>170),
        'blue_scales':(h>=100)&(h<=125)&(s>100)&(v>135),
        'green_burst':(h>=40)&(h<=91)&(s>130)&(v>135),
        'purple_ring':(h>=126)&(h<=148)&(s>120)&(v>130),
        'yellow_hex':(h>=21)&(h<=39)&(s>110)&(v>160),
        'red_single_eye':((h<=6)|(h>=168))&(s>140)&(v>140),
        # Eye-shape classification already distinguishes single/triple eyes.
        # Active glyphs can be red/pink; use the classifier's actual warm
        # support, then retain its class-equality gate in known localization.
        'orange_triple_eye':((h<=23)|(h>=162))&(s>100)&(v>130)}[name]


def _localized_known_glyph(image,item,expected):
    """Locate a measured known glyph near a drifting quad, for pose fitting only.

    The expanded patch may contain neighbouring tiles. Separate connected
    foreground components, require the original shape classifier to agree with
    the established atlas, and retain the existing 28px correspondence bound.
    This never contributes a node label or an empty/occupied cell vote.
    """
    q=item['quad'];expanded=q.mean(axis=0)+(q-q.mean(axis=0))*1.6
    patch=_crop(image,expanded)
    mask=_icon_mask(patch,expected).astype(np.uint8)
    count,labels,_,_=cv2.connectedComponentsWithStats(cv2.dilate(mask,np.ones((3,3),np.uint8)),8)
    inverse=cv2.getPerspectiveTransform(np.float32(((0,0),(95,0),(95,95),(0,95))),np.float32(expanded))
    candidates=[]
    for index in range(1,count):
        component=(labels==index)&(mask>0)
        yy,xx=np.where(component)
        if len(xx)<45:continue
        x1,x2,y1,y2=xx.min(),xx.max(),yy.min(),yy.max()
        width,height=x2-x1+1,y2-y1+1
        if min(width,height)<10 or max(width,height)>65 or len(xx)/(width*height)>.70:continue
        glyph=patch[y1:y2+1,x1:x2+1].copy()
        glyph[~component[y1:y2+1,x1:x2+1]]=0
        scale=58./max(width,height)
        nw,nh=max(1,round(width*scale)),max(1,round(height*scale))
        nx,ny=(96-nw)//2,(96-nh)//2
        normalized=np.zeros((96,96,3),np.uint8)
        normalized[ny:ny+nh,nx:nx+nw]=cv2.resize(glyph,(nw,nh),interpolation=cv2.INTER_AREA)
        evidence=classify_icon(normalized)
        if evidence.get('icon_id')!=expected or evidence.get('confidence',0)<.70:continue
        centre=np.array([[(np.percentile(xx,3)+np.percentile(xx,97))/2,
                          (np.percentile(yy,3)+np.percentile(yy,97))/2]],np.float32)
        point=cv2.perspectiveTransform(centre.reshape(1,1,2),inverse).reshape(2)
        distance=float(np.linalg.norm(point-item['centre']))
        if distance<=28:candidates.append((distance,point))
    return min(candidates,key=lambda value:value[0])[1] if candidates else None


class LayoutScanner:
    def __init__(self,target_detector=None,*,target_negative_evidence=False):
        if target_detector is not None and not callable(target_detector):
            raise TypeError('target_detector must be callable')
        self._target_detector=target_detector
        self.target_negative_evidence=bool(target_negative_evidence)
        self.cells = [dict(face=f,row=r,col=c) for f in FACES for r in range(3) for c in range(3)]
        self.rvec, self.tvec = SEED_R.copy(), SEED_T.copy()
        self.rotation = cv2.Rodrigues(self.rvec)[0]
        self.ready = False
        self.previous = None
        self.points = np.empty((0,2), np.float32)
        self.objects = np.empty((0,3), np.float64)
        self.orb = cv2.ORB_create(nfeatures=1200, edgeThreshold=12, fastThreshold=12)
        self.keyframes = []
        self.evidence = [[] for _ in self.cells]
        self.group, self.group_rotation = 0, None
        self.view_rotations=[]
        self.last_observation = {}
        self.last_targets = []
        self.last_projected = []
        self.quality = 0.
        self.last_error = "not_initialized"
        self.motion_px=0.
        self.pending_drag = None
        self.drag_in_progress=False
        self.last_drag_response=None
        self.response_axes = {}
        self.steering = None
        self.frames_seen = 0
        self.tracking_failures = 0
        self.best_known=0
        self.stagnant_frames=0
        self.icon_anchors={}
        self.identity_corrections=0
        self.correction_epoch=0
        self.glyph_anchor_at=None
        self.glyph_anchor_reason='not_initialized'
        self.glyph_anchor_rotation=None
        self.glyph_anchor_frame_id=None
        self.glyph_anchor_map_revision=None
        self.glyph_anchor_faces={}
        self._anchor_source={}
        self._frame_readings={}
        self._frame_associations={}
        self._postfit_visibility=None
        self._entity_anchor_proof=None
        self._target_coverage={}
        self.entity_association_diagnostic={}
        self.last_fused_result=None
        centres=np.array([_point(c) for c in self.cells])
        self._surface_points=centres
        self._surface_normals=np.array([BASES[c['face']][0] for c in self.cells],float)
        self._surface_geometry=np.array([np.vstack((_quad(c),_point(c))) for c in self.cells])
        for array in (self._surface_points,self._surface_normals,self._surface_geometry):
            array.flags.writeable=False
        self.symmetries=[]
        for axes in itertools.permutations(range(3)):
            for signs in itertools.product((-1,1),repeat=3):
                matrix=np.eye(3)[:,axes]*np.array(signs)
                if np.linalg.det(matrix)<.5:continue
                transformed=centres@matrix
                mapping=np.argmin(np.linalg.norm(transformed[:,None,:]-centres[None,:,:],axis=2),axis=1)
                self.symmetries.append((matrix,mapping))

    def _atlas_alignment(self,image,targets,allow_correction=True):
        """Detect 90-degree identity slips using the whole observed icon pattern."""
        if len(self.icon_anchors)<8:return True
        readings=[]
        for item in self._postfit_visible(image):
            centre=item['centre'];q=item['quad']
            if item['cosine']<.4 or item['area']<900:continue
            if np.any(q[:,0]<305) or np.any(q[:,0]>940) or np.any(q[:,1]<90) or np.any(q[:,1]>610):continue
            if 590<centre[0]<700 and 480<centre[1]<592:continue
            if any(np.linalg.norm(centre-np.array(t['point']))<60 for t in targets):continue
            _,icon=self._read_icon(image,q)
            if icon.get('icon_id') and icon.get('confidence',0)>=.65:
                readings.append((item['index'],icon['icon_id']))
        if len(readings)<6:return True
        candidates=[];current=None
        for matrix,mapping in self.symmetries:
            pairs=[(icon,self.icon_anchors[int(mapping[i])]) for i,icon in readings if int(mapping[i]) in self.icon_anchors]
            matches=sum(a==b for a,b in pairs);wrong=len(pairs)-matches
            candidate=(matches-2*wrong,matches,len(pairs),matrix)
            candidates.append(candidate)
            if np.allclose(matrix,np.eye(3)):current=candidate
        candidates.sort(key=lambda item:item[0],reverse=True)
        best=candidates[0];runner=candidates[1]
        if current is not None and best[0]>current[0]+3 and best[1]>=7 and best[1]/max(1,best[2])>=.85 and best[0]-runner[0]>=3:
            if not allow_correction:
                self.last_error='face_identity_conflict'
                return False
            matrix=best[3]
            self.rotation=self.rotation@matrix
            self.rvec=cv2.Rodrigues(self.rotation)[0]
            self.points=np.empty((0,2),np.float32);self.objects=np.empty((0,3))
            self.keyframes=[];self.pending_drag=None;self.response_axes={}
            self.identity_corrections+=1
            return True
        if current is not None and current[2]>=6 and current[1]/current[2]<.65:
            self.last_error='face_identity_conflict'
            return False
        return True

    def project(self, points, rotation=None):
        rv = self.rvec if rotation is None else cv2.Rodrigues(rotation)[0]
        return cv2.projectPoints(np.asarray(points,float), rv, self.tvec, K, None)[0].reshape(-1,2)

    def _begin_frame(self):
        # These caches belong to one owner and one image only. A pose correction
        # changes quad bytes, so its rectified pixels are recomputed.
        self._frame_readings={}
        self._frame_associations={}
        self._postfit_visibility=None
        self._entity_anchor_proof=None
        self._target_coverage={}
        self.entity_association_diagnostic={}

    def _target_observation(self,observation):
        """Only an explicit successful current-image model packet proves coverage."""
        packet=observation if isinstance(observation,dict) else {}
        targets=packet.get('targets',[]) if isinstance(observation,dict) else observation
        if not isinstance(targets,list):
            raise TypeError('target detector must return a target list or packet')
        self._target_coverage=dict(model_executed=packet.get('model_executed') is True,
            coverage_valid=packet.get('coverage_valid') is True,**self._anchor_source)
        return targets

    def _detect_targets(self,image):
        detector=detect_targets if self._target_detector is None else self._target_detector
        return self._target_observation(detector(image))

    def _entity_proof_token(self):
        return (self._anchor_source.get('source_frame_id'),
                self._anchor_source.get('source_frame_time'),
                self._anchor_source.get('source_map_revision'),int(self.identity_corrections),
                self.rvec.tobytes(),self.rotation.tobytes(),self.tvec.tobytes())

    def _same_frame_entity_pose(self):
        diagnostic=getattr(self,'refine_diagnostic',{})
        return bool(self._entity_anchor_proof is not None
            and self._entity_anchor_proof==self._entity_proof_token()
            and diagnostic.get('renewed')
            and all(diagnostic.get(key)==self._anchor_source.get(key)
                    for key in ('source_frame_id','source_frame_time','source_map_revision')))

    def _read_icon(self,image,quad):
        key=(id(image),id(classify_icon),image.shape,image.dtype.str,np.asarray(quad).tobytes())
        if key not in self._frame_readings:
            crop=_crop(image,quad)
            # Retain source/function objects, so recycled IDs cannot alias a
            # different image or a test-replaced classifier in the same frame.
            self._frame_readings[key]=(image,classify_icon,(crop,classify_icon(crop)))
        return self._frame_readings[key][2]

    def _associated_targets(self,targets,visible):
        target_values=tuple((target['kind'],tuple(target['point']),tuple(target['box']),
            target.get('confidence',0),target.get('confirmable',True),target.get('anchor_type'))
            for target in targets)
        method=self._associate_targets
        key=(getattr(method,'__func__',method),self._entity_proof_token(),
             self._same_frame_entity_pose(),target_values,
             tuple((item['index'],item.get('area')) for item in visible))
        if key not in self._frame_associations:
            self._frame_associations[key]=self._associate_targets(targets,visible)
        return self._frame_associations[key]

    def _postfit_visible(self,image,*,retain=False):
        """Reuse only this semantic source's exact post-fit geometry.

        Consumers receive private rows, so annotators or replaced readers cannot
        mutate another reader's quads. Ordinary callers never populate the cache.
        """
        method=self.visible
        dependency=getattr(method,'__func__',method)
        token=self._entity_proof_token()
        cached=self._postfit_visibility
        if (not retain and cached is not None and cached[0] is image
                and cached[1]==token and cached[2] is dependency):
            return deepcopy(cached[3])
        self._postfit_visibility=None
        rows=method()
        if retain:
            self._postfit_visibility=(image,token,dependency,deepcopy(rows))
        return rows

    def _record_glyph_anchor(self,reason,faces,cell_indices=None):
        """Record the image that proved alignment, never its processing finish."""
        self.glyph_anchor_at=float(self._anchor_source.get('source_frame_time',time.monotonic()))
        self.glyph_anchor_reason=reason
        self.glyph_anchor_rotation=self.rotation.copy()
        self.glyph_anchor_frame_id=self._anchor_source.get('source_frame_id')
        self.glyph_anchor_map_revision=self._anchor_source.get('source_map_revision',self.identity_corrections)
        self.glyph_anchor_faces=dict(Counter(faces))
        if hasattr(self,'refine_diagnostic'):
            self.refine_diagnostic['renewed']=True
            self.refine_diagnostic['accepted_faces']=dict(self.glyph_anchor_faces)
            if cell_indices is not None:
                indices=[int(i) for i in cell_indices]
                confirmed=set(self.refine_diagnostic.get('confirmed_cell_indices',()))
                self.refine_diagnostic['accepted_cell_indices']=indices
                self.refine_diagnostic['accepted_confirmed_cell_indices']=[i for i in indices if i in confirmed]
            if reason!='reset_bootstrap' and self._anchor_source.get('source_frame_id') is not None:
                self._entity_anchor_proof=self._entity_proof_token()

    def _anchor_observation(self):
        return dict(glyph_anchor_at=self.glyph_anchor_at,
                    glyph_anchor_rotation=(self.glyph_anchor_rotation.tolist()
                                           if self.glyph_anchor_rotation is not None else None),
                    glyph_anchor_frame_id=self.glyph_anchor_frame_id,
                    glyph_anchor_map_revision=self.glyph_anchor_map_revision,
                    glyph_anchor_faces=dict(self.glyph_anchor_faces),
                    glyph_anchor_reason=self.glyph_anchor_reason)

    def visible(self, rotation=None):
        rot = self.rotation if rotation is None else rotation
        camera = -(rot.T @ self.tvec).ravel()
        points=self._surface_points
        normals=self._surface_normals
        to_camera=camera-points
        cosines=np.einsum('ij,ij->i',normals,to_camera)/np.linalg.norm(to_camera,axis=1)
        # Project the same vertices in one OpenCV call instead of one call per
        # cell. The individual contour-area gates retain their original values.
        projected=self.project(self._surface_geometry.reshape(-1,3),rotation).reshape(-1,5,2)
        rows = []
        for index, cell in enumerate(self.cells):
            cosine = float(cosines[index])
            if cosine < .12:
                continue
            q = projected[index,:4]
            area = abs(cv2.contourArea(np.float32(q)))
            if area < 230 or not np.isfinite(q).all():
                continue
            rows.append(dict(index=index,quad=q,cosine=cosine,area=area,
                             centre=projected[index,4]))
        return rows

    def _bootstrap(self, image):
        # The seed is a geometry calibration, not permission to accept the pose.
        # Refit using centres of independently detected bright glyphs.
        obj, pixels, faces = [], [], []
        hsv = cv2.cvtColor(image,cv2.COLOR_RGB2HSV)
        glyph = (((hsv[:,:,1]>125)&(hsv[:,:,2]>155)) |
                 ((hsv[:,:,1]<65)&(hsv[:,:,2]>195))).astype(np.uint8)*255
        glyph &= _ui_mask()
        for item in self.visible():
            q = item['quad']
            centre = q.mean(axis=0)
            inner = centre + (q-centre)*.60
            mask = np.zeros(glyph.shape,np.uint8)
            cv2.fillConvexPoly(mask,np.int32(inner),255)
            y,x = np.where((glyph>0)&(mask>0))
            if len(x)<35:
                continue
            point = np.array((np.mean(x),np.mean(y)))
            if np.linalg.norm(point-item['centre'])>17:
                continue
            cell=self.cells[item['index']]
            obj.append(_point(cell));pixels.append(point);faces.append(cell['face'])
        if len(obj)<12 or len(set(faces))<2:
            self.last_error='reset_geometry_not_supported'
            return False
        rv,tv=self.rvec.copy(),self.tvec.copy()
        ok,rv,tv,inliers=cv2.solvePnPRansac(np.array(obj),np.array(pixels),K,None,
            rvec=rv,tvec=tv,useExtrinsicGuess=True,iterationsCount=100,
            reprojectionError=7.,confidence=.995,flags=cv2.SOLVEPNP_ITERATIVE)
        if not ok or inliers is None or len(inliers)<12:
            self.last_error='reset_geometry_fit_failed'
            return False
        ids=inliers.ravel()
        if len({faces[i] for i in ids})<2:
            return False
        rot=cv2.Rodrigues(rv)[0]
        if _angle(rot,self.rotation)>12 or abs(float(tv[2,0])-float(self.tvec[2,0]))>3:
            self.last_error='reset_geometry_outside_calibration'
            return False
        self.rvec,self.tvec,self.rotation=rv,tv,rot
        self.quality=min(.92,len(ids)/max(1,len(obj)))
        self.pivot_reference=tv.copy()
        self.ready=True
        self._record_glyph_anchor('reset_bootstrap',[faces[i] for i in ids])
        return True

    def _surface_mask(self, targets):
        mask=np.zeros((720,1280),np.uint8)
        for item in self.visible():
            if item['cosine']<.22:continue
            q=item['quad']; centre=q.mean(axis=0)
            cv2.fillConvexPoly(mask,np.int32(centre+(q-centre)*.83),255)
        mask &= _ui_mask()
        for target in targets:
            x,y,w,h=map(int,target['box'])
            cv2.rectangle(mask,(max(0,x-12),max(0,y-12)),(x+w+12,y+h+12),0,-1)
        return mask

    def _unproject(self, points, return_indices=False):
        """Associate visible surface features; vectorize convex-quad ray casts."""
        pixels=np.asarray(points,float).reshape(-1,2)
        visible=[item for item in self.visible() if item['cosine']>.22]
        if not len(pixels) or not visible:
            result=(np.empty((0,2),np.float32),np.empty((0,3),float))
            return (*result,[]) if return_indices else result
        # pointPolygonTest takes float32 contours and Point2f input. Preserve
        # that quantization before double-precision cross products; boundary
        # points are included, for either clockwise or anticlockwise quads.
        quads=np.asarray([item['quad'] for item in visible],np.float32).astype(float)
        queries=pixels.astype(np.float32).astype(float)
        edges=np.roll(quads,-1,axis=1)-quads
        offsets=queries[:,None,None,:]-quads[None,:,:,:]
        cross=edges[None,:,:,0]*offsets[:,:,:,1]-edges[None,:,:,1]*offsets[:,:,:,0]
        contained=np.all(cross>=0,axis=2)|np.all(cross<=0,axis=2)
        camera=-(self.rotation.T@self.tvec).ravel()
        rays=np.column_stack(((pixels[:,0]-640)/K[0,0],
                              (pixels[:,1]-360)/K[1,1],np.ones(len(pixels))))@self.rotation
        normals=np.asarray([BASES[self.cells[item['index']]['face']][0] for item in visible],float)
        denominator=rays@normals.T
        eligible=contained&(np.abs(denominator)>=1e-8)
        distances=np.full(denominator.shape,np.inf)
        np.divide(DISTANCE-normals@camera,denominator,out=distances,where=eligible)
        distances[distances<=0]=np.inf
        choice=np.argmin(distances,axis=1)
        nearest=distances[np.arange(len(pixels)),choice]
        indices=np.flatnonzero(np.isfinite(nearest))
        objects=camera+nearest[indices,None]*rays[indices]
        result=(np.asarray(pixels[indices],np.float32),objects)
        return (*result,indices.tolist()) if return_indices else result

    def _seed_features(self, gray, targets, save_keyframe=False):
        mask=self._surface_mask(targets)
        keep=[]
        for i,p in enumerate(self.points):
            x,y=map(int,np.round(p))
            if 0<=x<1280 and 0<=y<720 and mask[y,x]:keep.append(i)
        self.points=self.points[keep];self.objects=self.objects[keep]
        # Retain original 3D feature coordinates throughout their track. Casting
        # every feature back onto the approximate mesh each frame causes drift.
        for p in self.points:cv2.circle(mask,tuple(np.int32(p)),9,0,-1)
        if len(self.points)<250:
            points=cv2.goodFeaturesToTrack(gray,max(1,400-len(self.points)),.012,7,mask=mask,blockSize=5)
            if points is not None:
                newp,newo=self._unproject(points.reshape(-1,2))
                self.points=np.concatenate((self.points,newp))
                self.objects=np.concatenate((self.objects,newo))
        self.previous=gray
        if save_keyframe:
            keys,desc=self.orb.detectAndCompute(gray,self._surface_mask(targets))
            if desc is not None:
                _,objects,indices=self._unproject([key.pt for key in keys],return_indices=True)
                if len(indices)>=15:
                    self.keyframes.append(dict(descriptors=desc[indices],
                        objects=objects,rotation=self.rotation.copy()))
                    self.keyframes=self.keyframes[-24:]

    def _fit_pose(self, objects, points, limit=30):
        if len(points)<12:
            self.last_error=f'insufficient_correspondences:{len(points)}';return False
        # Idle animation changes apparent centre/scale even with no drag.
        # Fit only inside absolute reset-relative bounds, so translation cannot
        # accumulate freely and explain background features as cube rotation.
        rv,tv,errors=self._bounded_pose_fit(objects,points)
        inside=np.flatnonzero(errors<5.).reshape(-1,1)
        if len(inside)<12 or len(inside)/len(points)<.45:
            self.last_error=f'pose_inliers:{len(inside)}/{len(points)}';return False
        rot=cv2.Rodrigues(rv)[0]
        if _angle(rot,self.rotation)>limit:
            self.last_error=f'pose_jump:{_angle(rot,self.rotation):.1f}';return False
        xyz=np.asarray(objects)[inside.ravel()]
        pixels=np.asarray(points,np.float32)[inside.ravel()]
        if (np.linalg.norm(np.ptp(xyz,axis=0))<.6 or
                np.min(np.ptp(pixels,axis=0))<60 or
                cv2.contourArea(cv2.convexHull(pixels))<1500):
            self.last_error='pose_support_too_local';return False
        self.rvec,self.tvec,self.rotation=rv,tv,rot
        self.quality=min(.97,len(inside)/len(points))
        self.points=np.asarray(points,np.float32)[inside.ravel()]
        self.objects=np.asarray(objects,float)[inside.ravel()]
        return True

    def _bounded_pose_fit(self,objects,points):
        """Allow bounded idle centre/scale animation, always anchored to reset."""
        objects=np.asarray(objects,float);points=np.asarray(points,float)
        rv=self.rvec.copy();tv=self.tvec.copy()
        anchor=getattr(self,'pivot_reference',self.tvec)
        # Absolute bounds prevent incremental translation drift. Twelve pixels
        # permits small centre motion; +/-3.5% depth covers measured idle scale
        # changes. These bounds never follow the previous accepted translation.
        bounds=12.*float(anchor[2,0])/np.diag(K)[:2]
        for _ in range(16):
            projected,jac=cv2.projectPoints(objects,rv,tv,K,None)
            residual=(points-projected.reshape(-1,2)).reshape(-1)
            lengths=np.linalg.norm(residual.reshape(-1,2),axis=1)
            weight=np.repeat(np.sqrt(np.minimum(1.,3./np.maximum(lengths,1e-3))),2)
            matrix=jac[:,:6]*weight[:,None]
            rhs=residual*weight
            lower=np.r_[np.full(3,-np.inf),anchor[:2,0]-bounds,anchor[2,0]*.965]
            upper=np.r_[np.full(3,np.inf),anchor[:2,0]+bounds,anchor[2,0]*1.035]
            state=np.r_[rv.ravel(),tv.ravel()]
            # Solve the constrained linear step. Simply clipping translation
            # after an unconstrained solve leaves rotation compensating for a
            # depth change that never happened, and diverges near frontal faces.
            step=np.zeros(6);free=np.ones(6,dtype=bool)
            for _active in range(4):
                step[free]=np.linalg.lstsq(matrix[:,free],rhs-matrix[:,~free]@step[~free],rcond=None)[0]
                outside=free&((state+step<lower)|(state+step>upper))
                if not np.any(outside):break
                step[outside]=np.clip(state[outside]+step[outside],lower[outside],upper[outside])-state[outside]
                free[outside]=False
            length=np.linalg.norm(step[:3])
            if length>.15:step*=.15/length
            old_cost=float(np.sum(np.where(lengths<=3,lengths**2,6*lengths-9)))
            accepted=False
            for fraction in (1.,.5,.25,.125):
                candidate=np.clip(state+step*fraction,lower,upper)
                predicted=cv2.projectPoints(objects,candidate[:3],candidate[3:],K,None)[0].reshape(-1,2)
                error=np.linalg.norm(points-predicted,axis=1)
                cost=float(np.sum(np.where(error<=3,error**2,6*error-9)))
                if cost<=old_cost:
                    rv=candidate[:3].reshape(3,1);tv=candidate[3:].reshape(3,1)
                    accepted=True;break
            if not accepted or np.linalg.norm(step)<1e-5:break
        projected=cv2.projectPoints(objects,rv,tv,K,None)[0].reshape(-1,2)
        return rv,tv,np.linalg.norm(points-projected,axis=1)

    def _rotation_fit(self,objects,points):
        objects=np.asarray(objects,float);points=np.asarray(points,float)
        rv=self.rvec.copy()
        for _ in range(12):
            projected,jac=cv2.projectPoints(objects,rv,self.tvec,K,None)
            residual=(points-projected.reshape(-1,2)).reshape(-1)
            lengths=np.linalg.norm(residual.reshape(-1,2),axis=1)
            weight=np.repeat(np.sqrt(np.minimum(1.,4./np.maximum(lengths,1e-3))),2)
            matrix=jac[:,:3]*weight[:,None]
            step=np.linalg.lstsq(matrix,residual*weight,rcond=None)[0]
            length=np.linalg.norm(step)
            if length>.15:step*=.15/length
            rv+=step.reshape(3,1)
            if length<1e-5:break
        points2=cv2.projectPoints(objects,rv,self.tvec,K,None)[0].reshape(-1,2)
        return rv,np.linalg.norm(points-points2,axis=1)

    def _track(self,gray):
        if self.previous is None or len(self.points)<12:return False
        nextp,status,_=cv2.calcOpticalFlowPyrLK(self.previous,gray,self.points,None,
            winSize=(25,25),maxLevel=4,criteria=(cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,30,.01))
        if nextp is None:return False
        back,reverse,_=cv2.calcOpticalFlowPyrLK(gray,self.previous,nextp,None,winSize=(25,25),maxLevel=4)
        if back is None:return False
        good=(status.ravel()>0)&(reverse.ravel()>0)&(np.linalg.norm(back-self.points,axis=1)<1.8)
        if np.any(good):self.motion_px=float(np.median(np.linalg.norm(nextp[good]-self.points[good],axis=1)))
        return self._fit_pose(self.objects[good],nextp[good])

    def _refine_centres(self,image,targets):
        # A correctly classified patch can still include coloured tile seams.
        # Retry measured known-glyph components only after the ordinary fit
        # rejects, using this same RGB and all the original fitting gates.
        before=(self.rvec.copy(),self.tvec.copy(),self.rotation.copy())
        self._refine_centres_once(image,targets)
        initial=self.refine_diagnostic
        if (initial.get('reason')!='seven_inliers_missing' or initial.get('renewed') or
                not all(np.array_equal(a,b) for a,b in
                        zip(before,(self.rvec,self.tvec,self.rotation)))):
            return
        self._refine_centres_once(image,targets,localized_matching=True)
        self.refine_diagnostic['matching_localization_retry']=dict(
            attempts=1,initial_reason=initial['reason'],
            initial_candidates=initial['candidates'],initial_confirmed=initial['confirmed'])

    def _refine_centres_once(self,image,targets,*,localized_matching=False):
        """Anchor accumulated flow to the actual glyph grid, not tile sidewalls."""
        obj,pixels,confirmed,cell_ids=[],[],[],[]
        self.refine_diagnostic=dict(candidates=0,confirmed=0,reason="insufficient_centres",
            renewed=False,candidate_faces={},confirmed_faces={},
            candidate_cell_indices=[],confirmed_cell_indices=[],localized_known_cell_indices=[],**self._anchor_source)
        for item in self.visible():
            if item['cosine']<.30 or item['area']<900:continue
            q=item['quad'];centre=item['centre']
            if 585<centre[0]<700 and 470<centre[1]<595:continue
            if any(np.linalg.norm(centre-np.array(t['point']))<65 for t in targets):continue
            crop,classification=self._read_icon(image,q)
            known=self.icon_anchors.get(item['index'])
            matching=(known and classification.get('icon_id')==known and
                      classification.get('confidence',0)>=.70)
            if known and (classification.get('icon_id')!=known or
                          (localized_matching and matching)):
                localized=_localized_known_glyph(image,item,known)
                if localized is not None:
                    obj.append(_point(self.cells[item['index']]));pixels.append(localized)
                    cell_ids.append(item['index']);confirmed.append(True)
                    self.refine_diagnostic['localized_known_cell_indices'].append(item['index'])
                    continue
            if not classification.get('icon_id'):continue
            mask=_icon_mask(crop,classification['icon_id']).astype(np.uint8)
            mask[:10]=0;mask[86:]=0;mask[:,:10]=0;mask[:,86:]=0
            y,x=np.where(mask)
            if len(x)<50:continue
            # Midrange rather than center-of-mass: triangle glyphs are asymmetric.
            p=np.array([[(np.percentile(x,3)+np.percentile(x,97))/2,
                          (np.percentile(y,3)+np.percentile(y,97))/2]],np.float32)
            inverse=cv2.getPerspectiveTransform(np.float32(((0,0),(95,0),(95,95),(0,95))),np.float32(q))
            p=cv2.perspectiveTransform(p.reshape(1,1,2),inverse).reshape(2)
            if np.linalg.norm(p-centre)>28:continue
            obj.append(_point(self.cells[item['index']]));pixels.append(p)
            cell_ids.append(item['index'])
            confirmed.append(self.icon_anchors.get(item['index'])==classification['icon_id'] and classification.get('confidence',0)>=.70)
        self.refine_diagnostic.update(candidates=len(obj),confirmed=sum(confirmed),
            candidate_faces=dict(Counter(self.cells[i]['face'] for i in cell_ids)),
            confirmed_faces=dict(Counter(self.cells[i]['face'] for i,c in zip(cell_ids,confirmed) if c)),
            candidate_cell_indices=list(cell_ids),
            confirmed_cell_indices=[i for i,c in zip(cell_ids,confirmed) if c])
        confirmed_ids=np.flatnonzero(confirmed)
        if len(confirmed_ids)>=6:
            support_ids=confirmed_ids.copy()
            objects=np.asarray(obj)[confirmed_ids];points=np.asarray(pixels)[confirmed_ids]
            broad=_multi_face_grid_support(objects,points)
            if broad and hasattr(self,'pivot_reference'):
                before=np.linalg.norm(self.project(objects)-points,axis=1)
                rv,tv,error=self._bounded_pose_fit(objects,points)
                inliers=error<=6.
                if not np.all(inliers) and _multi_face_grid_support(objects[inliers],points[inliers]):
                    support_ids=support_ids[inliers]
                    objects,points=objects[inliers],points[inliers]
                    before=np.linalg.norm(self.project(objects)-points,axis=1)
                    rv,tv,error=self._bounded_pose_fit(objects,points)
                self.refine_diagnostic['joint_confirmed_inliers']=len(objects)
                rotation=cv2.Rodrigues(rv)[0]
                angle=_angle(rotation,self.rotation)
                pixel_delta=(tv[:2,0]-self.tvec[:2,0])*np.diag(K)[:2]/float(self.tvec[2,0])
                depth_delta=abs(float(tv[2,0]-self.tvec[2,0]))/float(self.pivot_reference[2,0])
                supported=(np.max(error)<=6 and np.median(error)<=3 and angle<=4 and
                           np.linalg.norm(pixel_delta)<=12 and depth_delta<=.035)
                improvement=np.median(error)<=max(.5,np.median(before)*.85)
                if supported and improvement:
                    self.rvec,self.tvec,self.rotation=rv,tv,rotation
                    self._record_glyph_anchor('known_multi_face_joint_fit',
                        [self.cells[cell_ids[i]]['face'] for i in support_ids],
                        [cell_ids[i] for i in support_ids])
                    self.refine_diagnostic.update(reason=self.glyph_anchor_reason,angle_deg=angle,
                        median_before=float(np.median(before)),median_after=float(np.median(error)),
                        max_after=float(np.max(error)),translation_pixels=pixel_delta.tolist(),
                        depth_delta_fraction=depth_delta)
                    self.points=np.empty((0,2),np.float32);self.objects=np.empty((0,3))
                    return
                if np.max(before)<=4 and np.median(before)<=3:
                    self._record_glyph_anchor('known_multi_face_grid_verified',
                        [self.cells[cell_ids[i]]['face'] for i in support_ids],
                        [cell_ids[i] for i in support_ids])
                    self.refine_diagnostic.update(reason=self.glyph_anchor_reason,
                        median_before=float(np.median(before)),median_after=float(np.median(before)))
                    return
                self.refine_diagnostic['joint_rejected']=dict(angle_deg=angle,
                    median_before=float(np.median(before)),median_after=float(np.median(error)),
                    max_after=float(np.max(error)),translation_pixels=pixel_delta.tolist())
        if len(obj)<7:
            if sum(confirmed)<4:return
            ids=np.flatnonzero(confirmed)
            objects=np.asarray(obj)[ids];points=np.asarray(pixels)[ids]
            # A low-count correction needs established matching atlas labels,
            # broad 2D support and a consensus fit; new labels retain seven.
            if np.min(np.ptp(points,axis=0))<80 or cv2.contourArea(cv2.convexHull(np.float32(points)))<7000:
                self.refine_diagnostic['reason']='confirmed_support_too_local';return
            candidates=[]
            for subset in itertools.combinations(range(len(points)),4):
                rv,_=self._rotation_fit(objects[list(subset)],points[list(subset)])
                projected=cv2.projectPoints(objects,rv,self.tvec,K,None)[0].reshape(-1,2)
                error=np.linalg.norm(points-projected,axis=1)
                inside=error<6.
                if np.count_nonzero(inside)>=4:
                    candidates.append((int(inside.sum()),-float(np.median(error[inside])),rv,inside))
            if not candidates:
                self.refine_diagnostic['reason']='confirmed_no_consensus';return
            _,_,rv,inside=max(candidates,key=lambda x:(x[0],x[1]))
            rv,error=self._rotation_fit(objects[inside],points[inside])
            rotation=cv2.Rodrigues(rv)[0]
            before=np.linalg.norm(points[inside]-self.project(objects[inside]),axis=1)
            angle=_angle(rotation,self.rotation)
            if (np.max(error)>=6 or np.median(error)>4 or angle>4 or
                    np.median(error)>np.median(before)*.85):
                self.refine_diagnostic['reason']='confirmed_residual_gate';return
            self.rvec,self.rotation=rv,rotation
            self._record_glyph_anchor('known_four_glyph_consensus',
                [self.cells[cell_ids[i]]['face'] for i in ids[inside]],
                [cell_ids[i] for i in ids[inside]])
            self.refine_diagnostic['reason']='confirmed_consensus'
            self.refine_diagnostic.update(angle_deg=angle,median_before=float(np.median(before)),median_after=float(np.median(error)))
            if angle>1.:
                self.points=np.empty((0,2),np.float32);self.objects=np.empty((0,3))
            return
        old_rotation=self.rotation.copy()
        rv,errors=self._rotation_fit(obj,pixels)
        if np.count_nonzero(errors<9)<7:
            self.refine_diagnostic['reason']='seven_inliers_missing';return
        rotation=cv2.Rodrigues(rv)[0]
        if _angle(rotation,old_rotation)>8:
            self.refine_diagnostic['reason']='correction_too_large';return
        self.refine_diagnostic['reason']='seven_centres'
        self.rvec,self.rotation=rv,rotation
        # Even a tiny fitted correction can provide independent grid evidence.
        # New labels need multi-face support; known atlas labels can also prove
        # a broad single-face grid with seven independently matching glyphs.
        face_counts=Counter(tuple(np.sign(point[np.argmax(np.abs(point))])*np.eye(3)[np.argmax(np.abs(point))])
                            for point in obj)
        points=np.asarray(pixels)
        independent=(sum(count>=2 for count in face_counts.values())>=2 or sum(confirmed)>=7)
        inlier_errors=errors[errors<9.]
        if (independent and np.min(np.ptp(points,axis=0))>=80 and
                cv2.contourArea(cv2.convexHull(np.float32(points)))>=7000 and
                len(inlier_errors)>=7 and np.median(inlier_errors)<=5 and
                _angle(rotation,old_rotation)<=4):
            self._record_glyph_anchor('independent_seven_glyph_grid',
                [self.cells[i]['face'] for i,err in zip(cell_ids,errors) if err<9.],
                [i for i,err in zip(cell_ids,errors) if err<9.])
        self.refine_diagnostic.update(median_after=float(np.median(errors)),
                                     max_after=float(np.max(errors)))
        # Start fresh feature rays after a model correction, never reuse old
        # inaccurate geometry observations as if they came from the corrected pose.
        if _angle(rotation,old_rotation)>1.0:
            self.points=np.empty((0,2),np.float32);self.objects=np.empty((0,3))

    def _relocalize(self,gray):
        keys,desc=self.orb.detectAndCompute(gray,_ui_mask())
        if desc is None:return False
        matcher=cv2.BFMatcher(cv2.NORM_HAMMING)
        for reference in reversed(self.keyframes):
            matches=matcher.knnMatch(reference['descriptors'],desc,k=2)
            pairs=[pair[0] for pair in matches if len(pair)==2 and pair[0].distance<.67*pair[1].distance]
            if len(pairs)<15:continue
            objects=reference['objects'][[m.queryIdx for m in pairs]]
            points=np.array([keys[m.trainIdx].pt for m in pairs])
            old_r,old_rotation=self.rvec.copy(),self.rotation.copy()
            self.rotation=reference['rotation'].copy()
            self.rvec=cv2.Rodrigues(self.rotation)[0]
            if self._fit_pose(objects,points,limit=45):return True
            self.rvec,self.rotation=old_r,old_rotation
        return False

    def note_drag(self,dx,dy):
        self.pending_drag=(float(dx),float(dy),self.rotation.copy())
        self.drag_in_progress=True
        self.last_drag_response=None

    def finish_drag(self,dx=None,dy=None):
        if self.pending_drag is not None and dx is not None and dy is not None:
            self.pending_drag=(float(dx),float(dy),self.pending_drag[2])
        self.drag_in_progress=False

    def _learn_response(self):
        if self.pending_drag is None or self.drag_in_progress:return
        dx,dy,previous=self.pending_drag
        delta=cv2.Rodrigues(self.rotation @ previous.T)[0].ravel()
        if np.linalg.norm(delta)<math.radians(.3):return
        axis=0 if abs(dx)>=abs(dy) else 1
        step=(dx,dy)[axis]
        if abs(step)>1:
            rate=delta/step
            old=self.response_axes.get(axis)
            self.response_axes[axis]=rate if old is None else .5*old+.5*rate
            self.last_drag_response=dict(axis=axis,pixels=step,
                angle_deg=float(np.degrees(np.linalg.norm(delta))),rotation_vector=delta.tolist())
        self.pending_drag=None

    def _associate_targets(self,targets,visible):
        assigned={}; uncertain=set()
        if not visible:return self._guard_entity_associations(assigned,uncertain)
        indices=np.array([item['index'] for item in visible])
        points=self._surface_points[indices]
        normals=self._surface_normals[indices]
        scales=np.maximum(20.,np.sqrt([item['area'] for item in visible]))
        corridors={}
        for target in targets:
            p=np.asarray(target['point'],float)
            heights=_anchor_heights(target)
            if heights not in corridors:
                samples=points[:,None,:]+normals[:,None,:]*np.linspace(*heights,16)[None,:,None]
                corridors[heights]=self.project(samples.reshape(-1,3)).reshape(-1,16,2)
            errors=np.linalg.norm(corridors[heights]-p,axis=2).min(axis=1)/scales
            ranks=[(float(error),int(index)) for error,index in zip(errors,indices)]
            ranks.sort()
            if not ranks:continue
            close=[i for error,i in ranks if error<.5]
            uncertain.update(close)
            margin=ranks[1][0]-ranks[0][0] if len(ranks)>1 else 1.
            if (ranks[0][0]<.30 and margin>.12 and target.get('confidence',0)>=.6
                    and target.get('confirmable', True)):
                index=ranks[0][1]
                entry=dict(target,association_confidence=max(.55,1-ranks[0][0]),cell_index=index)
                if index in assigned and assigned[index]['kind']!=entry['kind']:
                    assigned[index]={'kind':'unknown','confidence':0.,'association_confidence':0.}
                else:assigned[index]=entry
        return self._guard_entity_associations(assigned,uncertain)

    def _guard_entity_associations(self,assigned,uncertain):
        """Current glyph proof authorizes positives; every surface may veto them."""
        uncertain=set(uncertain)
        renewed=self._same_frame_entity_pose()
        diagnostic=dict(**self._anchor_source,renewed=renewed,decisions=[])
        self.entity_association_diagnostic=diagnostic
        if not renewed:
            for index,entry in assigned.items():
                uncertain.add(index)
                diagnostic['decisions'].append(dict(index=index,kind=entry.get('kind'),
                    reason='same_frame_glyph_pose_not_renewed'))
            return {},uncertain
        if not assigned:return assigned,uncertain
        xyz=self._surface_geometry[:,:4,:].reshape(-1,3)
        camera_xyz=xyz@self.rotation.T+self.tvec.ravel()
        projected=self.project(xyz).reshape(-1,4,2)
        valid=np.isfinite(projected).all(axis=(1,2))&(camera_xyz[:,2].reshape(-1,4)>0).all(axis=1)
        scales=np.array([max(20.,np.sqrt(abs(cv2.contourArea(np.float32(quad)))))
                         if ok else np.inf for quad,ok in zip(projected,valid)])
        corridors={};kept={}
        for index,entry in assigned.items():
            if entry['kind']=='unknown':
                kept[index]=entry;continue
            heights=_anchor_heights(entry)
            if heights not in corridors:
                samples=self._surface_points[:,None,:]+self._surface_normals[:,None,:]*np.linspace(*heights,16)[None,:,None]
                camera_samples=samples@self.rotation.T+self.tvec.ravel()
                path=self.project(samples.reshape(-1,3)).reshape(-1,16,2)
                corridor_valid=valid&np.isfinite(path).all(axis=(1,2))&(camera_samples[:,:,2]>0).all(axis=1)
                corridors[heights]=(path,corridor_valid)
            path,corridor_valid=corridors[heights]
            errors=np.linalg.norm(path-np.asarray(entry['point']),axis=2).min(axis=1)/scales
            errors[~corridor_valid]=np.inf
            competitor=int(np.argmin(np.where(np.arange(len(errors))!=index,errors,np.inf)))
            margin=float(errors[competitor]-errors[index])
            evidence=dict(**self._anchor_source,error=float(errors[index]),
                competitor_index=competitor,competitor_error=float(errors[competitor]),margin=margin,
                rvec=self.rvec.ravel().tolist(),rotation=self.rotation.tolist(),tvec=self.tvec.ravel().tolist())
            if not(errors[index]<.30 and margin>.12):
                uncertain.add(index)
                uncertain.update(i for i,error in enumerate(errors) if error<.5)
                diagnostic['decisions'].append(dict(index=index,kind=entry['kind'],
                    reason='all54_normal_corridor_competitor',**evidence))
            else:
                # A fit on a different face can track the cube while leaving
                # this oblique face's sprite corridor poorly constrained. Do
                # not turn that extrapolation into an occupancy vote.
                face=self.cells[index]['face']
                local_count=self.glyph_anchor_faces.get(face,0)
                accepted_count=self.refine_diagnostic.get('accepted_faces',{}).get(face,0)
                if (type(local_count) is not int or type(accepted_count) is not int
                        or min(local_count,accepted_count)<2):
                    uncertain.add(index)
                    uncertain.update(i for i,error in enumerate(errors) if error<.5)
                    diagnostic['decisions'].append(dict(index=index,kind=entry['kind'],
                        reason='target_face_glyph_support_required',face=face,
                        local_glyphs=local_count,**evidence))
                    continue
                from ._deep_dive_target_anchor_uncertainty import certify_target_anchor
                stable=certify_target_anchor(entry['point'],path,scales,index,valid=corridor_valid)
                if not stable['ready']:
                    uncertain.add(index)
                    uncertain.update(i for i,error in enumerate(errors) if error<.5)
                    diagnostic['decisions'].append(dict(index=index,kind=entry['kind'],
                        reason='target_anchor_pixel_uncertain',anchor_uncertainty=stable,**evidence))
                    continue
                evidence['target_face_glyph_count']=local_count
                evidence['anchor_uncertainty']=stable
                # A diffuse Boss box can move its centre while the actual core
                # stays fixed. At a height endpoint, the fitted corridor cannot
                # bracket that centre; do not treat its pixels as an exact core
                # anchor if a plausible central region also supports a rival.
                # Interior heights retain the original point-based decision.
                point=np.asarray(entry['point'],float)
                x,y,width,height=entry['box']
                sample=int(np.argmin(np.linalg.norm(path[index]-point,axis=1)))
                box_centre=np.array((x+width/2,y+height/2))
                if (entry['kind']=='singularity' and sample in (0,15)
                        and np.linalg.norm(point-box_centre)<=1.0):
                    radius=.20*max(0.,min(width,height))
                    pixel_errors=np.linalg.norm(path-point,axis=2).min(axis=1)
                    robust=np.maximum(0.,pixel_errors-radius)/scales
                    robust[~corridor_valid]=np.inf
                    rival=int(np.argmin(np.where(np.arange(len(robust))!=index,robust,np.inf)))
                    robust_margin=float(robust[rival]-robust[index])
                    reliability=dict(height_sample=sample,
                        fitted_height=float(np.linspace(*heights,16)[sample]),
                        centre_uncertainty_px=float(radius),competitor_index=rival,
                        competitor_error=float(robust[rival]),margin=robust_margin)
                    evidence['anchor_center_reliability']=reliability
                    if robust_margin<=.12:
                        uncertain.add(index)
                        uncertain.update(i for i,error in enumerate(robust) if error<.5)
                        diagnostic['decisions'].append(dict(index=index,kind=entry['kind'],
                            reason='bbox_center_height_boundary_ambiguous',**evidence))
                        continue
                kept[index]=dict(entry,association_evidence=evidence)
        return kept,uncertain

    def _observe_cells(self,image,frame_id,targets):
        visible=self._postfit_visible(image);self.last_projected=visible
        assigned,uncertain=self._associated_targets(targets,visible)
        for item in visible:
            i=item['index'];q=item['quad'];centre=item['centre']
            if np.any(q[:,0]<300) or np.any(q[:,0]>950) or np.any(q[:,1]<84) or np.any(q[:,1]>615):continue
            # Screen-fixed Reset View overlapping a tile invalidates its read.
            if cv2.pointPolygonTest(np.float32(q),(640.,534.),False)>=0 or (
                594<centre[0]<687 and 485<centre[1]<587):continue
            if item['cosine']<.30 or item['area']<800:continue
            observation=dict(frame_id=int(frame_id),group=self.group,quality=round(self.quality,3),
                quad=np.round(q,1).tolist(),cosine=round(item['cosine'],3))
            target=assigned.get(i)
            if target and target['kind']!='unknown':
                observation.update(occupant=target['kind'],icon_id=None,
                    confidence=round(min(target['confidence'],target['association_confidence'],self.quality),3),
                    target_box=deepcopy(target['box']),target_point=deepcopy(target['point']),
                    target_source=target.get('source'),target_box_evidence=target.get('box_evidence'),
                    association_evidence=deepcopy(target['association_evidence']))
                observation['anchor_type'] = target.get('anchor_type')
            elif i in uncertain:
                continue
            else:
                # Even a sprite assigned elsewhere can obscure this tile. Also
                # require the prospective normal corridor to be on-screen and
                # outside the HUD before accepting negative target evidence.
                blocked=False;any_target_overlap=False
                for target in targets:
                    x,y,w,h=target['box']
                    rectangle=np.float32(((x-8,y-8),(x+w+8,y-8),(x+w+8,y+h+8),(x-8,y+h+8)))
                    overlap,_=cv2.intersectConvexConvex(np.float32(q),rectangle)
                    any_target_overlap=any_target_overlap or overlap>0
                    if overlap>item['area']*.08:blocked=True;break
                normal=np.array(BASES[self.cells[i]['face']][0],float)
                corridor=self.project([_point(self.cells[i])+normal*h for h in (0.,.85)])
                if any(not(305<p[0]<940 and 95<p[1]<600) or
                       (590<p[0]<698 and 480<p[1]<590) for p in corridor):blocked=True
                if blocked:continue
                _,icon=self._read_icon(image,q)
                # Negative target evidence is accumulated from well-facing views.
                if item['cosine']<.48:continue
                if icon.get('icon_id') and icon.get('confidence',0)>=.6:
                    observation.update(occupant='none',icon_id=icon['icon_id'],
                        confidence=round(min(icon['confidence'],self.quality),3))
                else:
                    coverage=self._target_coverage
                    if not(self.target_negative_evidence and self._same_frame_entity_pose()
                           and coverage.get('model_executed') and coverage.get('coverage_valid')
                           and not any_target_overlap):continue
                    # Cover the largest supported entity height (Boss reaches
                    # 1.0), including the intermediate pixels of its corridor.
                    samples=_point(self.cells[i])+normal*np.linspace(0.,1.,16)[:,None]
                    path=self.project(samples)
                    camera_samples=samples@self.rotation.T+self.tvec.ravel()
                    if (not np.isfinite(path).all() or not(camera_samples[:,2]>0).all()
                            or any(not(305<p[0]<940 and 95<p[1]<600) or
                                (590<p[0]<698 and 480<p[1]<590) for p in path)):continue
                    observation.update(occupant='none',icon_id=None,
                        confidence=round(min(.8,self.quality),3),
                        occupancy_negative_evidence=dict(**coverage,
                            rvec=self.rvec.ravel().tolist(),rotation=self.rotation.tolist(),
                            tvec=self.tvec.ravel().tolist(),corridor=path.tolist()))
            group_entries=self.evidence[i]
            existing=next((j for j,x in enumerate(group_entries) if x['group']==self.group),None)
            if existing is None:group_entries.append(observation)
            else:
                # A visible attached target is stronger evidence than a later
                # missed detection at the same view. Cross-view negatives remain
                # available to contradict false positives during fusion.
                weight=lambda x:x['confidence']*(4 if x['occupant']!='none' else 1)
                if weight(observation)>weight(group_entries[existing]):group_entries[existing]=observation
            if len(group_entries)>48:
                positive=[x for x in group_entries if x['occupant']!='none']
                negative=[x for x in group_entries if x['occupant']=='none']
                self.evidence[i]=sorted(positive,key=lambda x:x['confidence'],reverse=True)[:24]+sorted(negative,key=lambda x:x['confidence'],reverse=True)[:24]

    def fork_semantic(self):
        """Copy atlas state without sharing the uncopyable OpenCV ORB instance."""
        other=LayoutScanner(target_detector=self._target_detector,
                            target_negative_evidence=self.target_negative_evidence)
        for name in ('evidence','view_rotations','group','group_rotation','icon_anchors',
                     'identity_corrections','correction_epoch','best_known','stagnant_frames','rvec','tvec',
                     'rotation','quality','ready','last_targets','last_projected',
                     'glyph_anchor_at','glyph_anchor_reason','glyph_anchor_rotation',
                     'glyph_anchor_frame_id','glyph_anchor_map_revision','glyph_anchor_faces'):
            setattr(other,name,deepcopy(getattr(self,name)))
        if hasattr(self,'pivot_reference'):
            other.pivot_reference=self.pivot_reference.copy()
        return other

    def pose_snapshot(self):
        """Copy geometry for another scanner; never share mutable pose arrays."""
        return dict(rvec=self.rvec.copy(),tvec=self.tvec.copy(),
                    rotation=self.rotation.copy(),quality=float(self.quality),
                    map_revision=int(self.identity_corrections),correction_epoch=int(self.correction_epoch),
                    pivot_reference=getattr(self,'pivot_reference',self.tvec).copy())

    def track_frame(self,image_rgb,frame_id=0,mask_targets=None,pose_correction=None):
        """Geometry-only hot path. Does not detect targets, classify or fuse cells.

        mask_targets may be current projected boxes or a dictionary containing
        mask_observation and age_sec, in which case boxes are reprojected after
        fitting this frame. This instance must be owned by the geometry worker.
        """
        if not isinstance(image_rgb,np.ndarray) or image_rgb.shape!=(720,1280,3) or image_rgb.dtype!=np.uint8:
            return dict(tracking_ok=False,quality=0.,reason='invalid_frame',pose_delta_deg=0.)
        if not self.ready:
            return dict(tracking_ok=False,quality=0.,reason='tracker_requires_bootstrap',pose_delta_deg=0.)
        gray=cv2.cvtColor(image_rgb,cv2.COLOR_RGB2GRAY)
        before=self.rotation.copy();self.motion_px=0.
        ok=self._track(gray)
        if not ok:
            track_error=self.last_error or 'tracking_lost'
            ok=self._relocalize(gray)
            if not ok:self.last_error=f'{track_error};relocalize:{self.last_error}'
        self.frames_seen+=1
        if not ok:
            self.tracking_failures+=1
            self.last_observation=dict(tracking_ok=False,quality=0.,reason=self.last_error,pose_delta_deg=0.)
            return self.last_observation
        correction_applied=False
        if pose_correction is not None and pose_correction.get('source_epoch')==self.correction_epoch:
            body=np.asarray(pose_correction.get('body_rotation'),float)
            translation=np.asarray(pose_correction.get('translation_delta',[0.,0.,0.]),float)
            valid_translation=translation.size==3 and np.isfinite(translation).all()
            candidate_tv=self.tvec+translation.reshape(3,1) if valid_translation else self.tvec
            anchor=getattr(self,'pivot_reference',self.tvec)
            bounds=12.*float(anchor[2,0])/np.diag(K)[:2]
            step_pixels=translation.ravel()[:2]*np.diag(K)[:2]/float(self.tvec[2,0]) if valid_translation else np.array([np.inf,np.inf])
            valid_translation=bool(valid_translation and np.linalg.norm(step_pixels)<=12.+1e-6 and
                abs(float(translation.ravel()[2]))<=float(anchor[2,0])*.035+1e-6 and
                np.all(np.abs(candidate_tv[:2,0]-anchor[:2,0])<=bounds+1e-6) and
                float(anchor[2,0])*.965-1e-6<=float(candidate_tv[2,0])<=float(anchor[2,0])*1.035+1e-6)
            if (body.shape==(3,3) and np.isfinite(body).all() and
                    np.allclose(body.T@body,np.eye(3),atol=1e-5) and
                    abs(float(np.linalg.det(body))-1.)<1e-5 and _angle(body,np.eye(3))<=8. and valid_translation):
                # The semantic frame may be old. A right/body correction carries
                # its model alignment through subsequent camera-space motion.
                self.rotation=self.rotation@body
                self.rvec=cv2.Rodrigues(self.rotation)[0]
                self.tvec=candidate_tv
                if pose_correction.get('glyph_anchor_at') is not None:
                    self.glyph_anchor_at=float(pose_correction['glyph_anchor_at'])
                    self.glyph_anchor_reason=pose_correction.get('glyph_anchor_reason','semantic_grid_correction')
                    rotation=pose_correction.get('glyph_anchor_rotation')
                    self.glyph_anchor_rotation=np.asarray(rotation,float).copy() if rotation is not None else None
                    self.glyph_anchor_frame_id=pose_correction.get('glyph_anchor_frame_id')
                    self.glyph_anchor_map_revision=pose_correction.get('glyph_anchor_map_revision')
                    self.glyph_anchor_faces=dict(pose_correction.get('glyph_anchor_faces',{}))
                self.correction_epoch+=1
                self.points=np.empty((0,2),np.float32);self.objects=np.empty((0,3))
                self.keyframes=[];self._stream_keyframe_rotation=None
                correction_applied=True
        targets=(self.project_target_masks(mask_targets['mask_observation'],mask_targets.get('age_sec',0.))
                 if isinstance(mask_targets,dict) else (mask_targets or []))
        saved=getattr(self,'_stream_keyframe_rotation',None)
        new_keyframe=saved is None or _angle(self.rotation,saved)>=10.
        self._seed_features(gray,targets,save_keyframe=new_keyframe)
        if new_keyframe:self._stream_keyframe_rotation=self.rotation.copy()
        self.last_projected=self.visible();self.last_targets=targets;self.last_error=''
        self.last_observation=dict(tracking_ok=True,quality=float(self.quality),
            pose_delta_deg=_angle(self.rotation,before),motion_px=float(self.motion_px),
            feature_count=len(self.points),pose=self.pose_snapshot(),frame_id=int(frame_id),during_drag=True,
            correction_applied=correction_applied)
        return self.last_observation

    def semantic_view(self,image_rgb,frame_id,pose_snapshot,targets=None,*,source_frame_time=None):
        """Fuse one image using its frozen pose on a separate semantic scanner.

        Small glyph-grid corrections are fitted on this same frozen image and
        returned as epoch-tagged body rotations. Identity changes are rejected.
        """
        semantic_started=time.perf_counter()
        semantic_cost={}
        def measured_observation(observation):
            semantic_cost['total']=time.perf_counter()-semantic_started
            observation['semantic_cost_sec']=semantic_cost
            self._postfit_visibility=None
            return observation
        if not isinstance(image_rgb,np.ndarray) or image_rgb.shape!=(720,1280,3) or image_rgb.dtype!=np.uint8:
            return measured_observation(dict(tracking_ok=False,quality=0.,reason='invalid_frame'))
        self._begin_frame()
        self.last_fused_result=None
        self._anchor_source=dict(source_frame_id=int(frame_id),
            source_frame_time=float(time.monotonic() if source_frame_time is None else source_frame_time),
            source_map_revision=int(pose_snapshot.get('map_revision',self.identity_corrections)))
        self.rvec=np.asarray(pose_snapshot['rvec'],float).reshape(3,1).copy()
        self.tvec=np.asarray(pose_snapshot['tvec'],float).reshape(3,1).copy()
        self.rotation=np.asarray(pose_snapshot['rotation'],float).reshape(3,3).copy()
        self.quality=float(pose_snapshot['quality']);self.ready=True
        self.correction_epoch=int(pose_snapshot.get('correction_epoch',0))
        if 'pivot_reference' in pose_snapshot:
            self.pivot_reference=np.asarray(pose_snapshot['pivot_reference'],float).reshape(3,1).copy()
        source_rotation=self.rotation.copy()
        source_translation=self.tvec.copy()
        stage_started=time.perf_counter()
        self.last_targets=self._detect_targets(image_rgb) if targets is None else self._target_observation(targets)
        semantic_cost['detect']=time.perf_counter()-stage_started
        stage_started=time.perf_counter()
        self._refine_centres(image_rgb,self.last_targets)
        semantic_cost['refine']=time.perf_counter()-stage_started
        body=source_rotation.T@self.rotation
        correction_angle=_angle(self.rotation,source_rotation)
        translation_delta=self.tvec-source_translation
        correction=(dict(body_rotation=body.tolist(),source_epoch=self.correction_epoch,
                          source_frame_id=int(frame_id),angle_deg=correction_angle,
                          translation_delta=translation_delta.ravel().tolist(),
                          **self._anchor_observation())
                    if correction_angle<=8. and (.1<correction_angle or np.linalg.norm(translation_delta)>1e-4) else None)
        self.last_projected=self._postfit_visible(image_rgb,retain=True);self.frames_seen+=1
        stage_started=time.perf_counter()
        aligned=self._atlas_alignment(image_rgb,self.last_targets,allow_correction=False)
        semantic_cost['atlas_alignment']=time.perf_counter()-stage_started
        if not aligned:
            self._entity_anchor_proof=None
            self.tracking_failures+=1
            self.last_observation=dict(tracking_ok=False,quality=self.quality,reason=self.last_error,frame_id=int(frame_id))
            return measured_observation(self.last_observation)
        distances=[_angle(self.rotation,r) for r in self.view_rotations]
        new_group=not distances or min(distances)>=8.
        if new_group:self.view_rotations.append(self.rotation.copy())
        self.group=len(self.view_rotations) if new_group else int(np.argmin(distances))+1
        self.group_rotation=self.view_rotations[self.group-1]
        anchor_age=time.monotonic()-(self.glyph_anchor_at or 0.)
        if anchor_age>6.:
            self.last_observation=dict(tracking_ok=False,quality=self.quality,
                reason='glyph_anchor_support_lost:'+str(self.refine_diagnostic.get('reason')),
                frame_id=int(frame_id),glyph_anchor_age_sec=anchor_age,
                refine_diagnostic=deepcopy(self.refine_diagnostic),**self._anchor_observation())
            return measured_observation(self.last_observation)
        fusion_paused=anchor_age>2.
        if not fusion_paused:
            stage_started=time.perf_counter()
            self._observe_cells(image_rgb,frame_id,self.last_targets)
            semantic_cost['associate_observe']=time.perf_counter()-stage_started
        stage_started=time.perf_counter()
        result=self.result()
        semantic_cost['result']=time.perf_counter()-stage_started
        self.last_fused_result=result
        stage_started=time.perf_counter()
        for i,cell in enumerate(result['cells']):
            if cell['occupant']=='none' and cell['node_status']=='known' and cell['confidence']>=.70:
                self.icon_anchors.setdefault(i,cell['icon_id'])
        self.last_error=''
        self.last_observation=dict(tracking_ok=True,quality=self.quality,frame_id=int(frame_id),
            known_cells=result['known_cells'],faces_observed=result['faces_observed'],view_group=self.group,
            mask_observation=self.target_mask_observation(),pose_correction=correction,
            pose=self.pose_snapshot(),fusion_paused=fusion_paused,glyph_anchor_age_sec=anchor_age,
            refine_diagnostic=deepcopy(self.refine_diagnostic),**self._anchor_observation())
        self.last_observation['entity_association_diagnostic']=deepcopy(self.entity_association_diagnostic)
        self.last_observation['target_coverage']=deepcopy(self._target_coverage)
        semantic_cost['observation']=time.perf_counter()-stage_started
        return measured_observation(self.last_observation)

    def target_mask_observation(self):
        """Describe masks in cube coordinates where association is trustworthy."""
        assigned,_=self._associated_targets(self.last_targets,self.last_projected)
        output=[]
        for target in self.last_targets:
            entry=dict(kind=target['kind'],box=list(target['box']),point=list(target['point']),
                       anchor_type=target.get('anchor_type'),confirmable=target.get('confirmable',True),
                       confidence=float(target.get('confidence',0)),source=target.get('source'),
                       box_evidence=target.get('box_evidence'))
            match=next((t for t in assigned.values() if t.get('kind')==target['kind']
                        and t.get('point')==target['point']),None)
            if match is not None:
                cell=self.cells[match['cell_index']];normal=np.asarray(BASES[cell['face']][0],float)
                heights=_anchor_heights(target)
                xyz=np.array([_point(cell)+normal*h for h in np.linspace(*heights,16)])
                index=int(np.argmin(np.linalg.norm(self.project(xyz)-np.asarray(target['point']),axis=1)))
                entry.update(object_point=xyz[index].tolist(),cell_index=match['cell_index'],
                             source_depth=float((self.rotation@xyz[index].reshape(3,1)+self.tvec)[2,0]),
                             association_evidence=deepcopy(match.get('association_evidence')))
            output.append(entry)
        return output

    def project_target_masks(self,observation,age_sec=0.,max_age_sec=.8):
        """Move recent semantic exclusions with the current pose; expire stale ones."""
        if age_sec<0 or age_sec>max_age_sec:return []
        output=[]
        for item in observation or []:
            x,y,w,h=item['box'];padding=12.+20.*age_sec
            if 'object_point' in item:
                xyz=np.asarray(item['object_point'],float)
                depth=float((self.rotation@xyz.reshape(3,1)+self.tvec)[2,0])
                if depth<=0:continue
                point=self.project([xyz])[0]
                scale=float(np.clip(item['source_depth']/depth,.7,1.4))
                w*=scale;h*=scale
                # Retain the detected point's offset within its source box.
                x=point[0]+(x-item['point'][0])*scale
                y=point[1]+(y-item['point'][1])*scale
            else:
                # Unassociated targets cannot be assigned fictitious depth.
                # Briefly expand their old screen exclusion, then expire it.
                padding+=60.*age_sec
                point=np.asarray(item['point'],float)
            output.append(dict(kind=item['kind'],point=point.tolist(),
                               box=[x-padding,y-padding,w+2*padding,h+2*padding]))
        return output

    def observe(self,image_rgb,frame_id=0,*,semantic=True):
        if not isinstance(image_rgb,np.ndarray) or image_rgb.shape!=(720,1280,3) or image_rgb.dtype!=np.uint8:
            return dict(tracking_ok=False,quality=0.,reason='invalid_frame',pose_delta_deg=0.)
        self._begin_frame()
        self._anchor_source=dict(source_frame_id=int(frame_id),source_frame_time=time.monotonic(),
            source_map_revision=int(self.identity_corrections))
        gray=cv2.cvtColor(image_rgb,cv2.COLOR_RGB2GRAY)
        self.motion_px=0.
        before=self.rotation.copy()
        targets=self._detect_targets(image_rgb)  # also mask animated targets during geometry-only tracking
        initial=not self.ready
        ok=self._bootstrap(image_rgb) if initial else self._track(gray)
        if not ok and self.ready:
            track_error=self.last_error or 'tracking_lost'
            ok=self._relocalize(gray)
            if not ok:self.last_error=f'{track_error};relocalize:{self.last_error}'
        self.frames_seen+=1
        if not ok:
            self.tracking_failures+=1
            self.last_observation=dict(tracking_ok=False,quality=0.,pose_delta_deg=0.,reason=self.last_error or 'tracking_lost')
            return self.last_observation
        self.last_error=''
        if semantic == 'auto':
            semantic=initial or not self.view_rotations or min(_angle(self.rotation,r) for r in self.view_rotations)>=8.
        if semantic and self.frames_seen%5==0:self._refine_centres(image_rgb,targets)
        delta=_angle(self.rotation,before)
        if semantic and not self._atlas_alignment(image_rgb,targets):
            self._entity_anchor_proof=None
            self.tracking_failures+=1
            return dict(tracking_ok=False,quality=0.,pose_delta_deg=0.,reason=self.last_error)
        self._learn_response()
        self.last_targets=targets
        if not semantic:
            self._seed_features(gray,targets,save_keyframe=False)
            self.last_projected=self.visible()
            self.last_observation=dict(tracking_ok=True,pose_delta_deg=round(delta,3),
                motion_px=round(self.motion_px,3),quality=round(self.quality,3),during_drag=True)
            return self.last_observation
        distances=[_angle(self.rotation,r) for r in self.view_rotations]
        new_group=not distances or min(distances)>=8.
        if new_group:self.view_rotations.append(self.rotation.copy())
        self.group=len(self.view_rotations) if new_group else int(np.argmin(distances))+1
        self.group_rotation=self.view_rotations[self.group-1]
        self.last_targets=targets
        self._observe_cells(image_rgb,frame_id,targets)
        self._seed_features(gray,targets,save_keyframe=initial or new_group)
        result=self.result()
        for i,cell in enumerate(result['cells']):
            if cell['occupant']=='none' and cell['node_status']=='known' and cell['confidence']>=.70:
                self.icon_anchors.setdefault(i,cell['icon_id'])
        if result['known_cells']>self.best_known:
            self.best_known=result['known_cells'];self.stagnant_frames=0
        else:self.stagnant_frames+=1
        self.last_observation=dict(tracking_ok=True,pose_delta_deg=round(delta,3),quality=round(self.quality,3),
            motion_px=round(self.motion_px,3),
            faces_observed=result['faces_observed'],known_cells=result['known_cells'],
            feature_count=len(self.points),view_group=self.group)
        starts=[x for x in self.visible() if x['cosine']>.4 and
                400<x['centre'][0]<860 and 180<x['centre'][1]<460 and
                all(np.linalg.norm(x['centre']-np.array(t['point']))>50 for t in targets)]
        if starts:
            point=min(starts,key=lambda x:np.linalg.norm(x['centre']-np.array((640,330))))['centre']
            self.last_observation['drag_start']=np.round(point).astype(int).tolist()
        return self.last_observation

    def result(self):
        cells=[]
        for cell,observations in zip(self.cells,self.evidence):
            row=dict(cell,occupant='unknown',occupant_status='unknown',icon_id=None,node_kind=None,
                     node_status='unknown',confidence=0.,evidence=[],geometric_neighbors=_neighbors(cell))
            pool=observations
            votes=Counter(o['occupant'] for o in pool)
            if votes:
                scores=Counter()
                for o in pool:scores[o['occupant']]+=o['confidence']*(4 if o['occupant']!='none' else 1)
                kind,_=scores.most_common(1)[0];count=votes[kind]
                agreeing=[o for o in pool if o['occupant']==kind]
                required=2 if kind!='none' else 3
                fraction=scores[kind]/sum(scores.values())
                conflict=len(votes)>1 and fraction<.72
                row['occupant_evidence_counts']=dict(votes)
                row['occupant_evidence_scores']={k:round(v,2) for k,v in scores.items()}
                positive_pair = (_independent_positive_witness(agreeing, kind, self.identity_corrections)
                                 if kind!='none' and count>=required and not conflict else [])
                if count>=required and not conflict and (kind=='none' or positive_pair):
                    row.update(occupant=kind,occupant_status='confirmed',
                               confidence=round(sum(o['confidence'] for o in agreeing)/len(agreeing),3))
                    if kind!='none':row['node_status']='not_required_target'
                    else:
                        icons=Counter(o['icon_id'] for o in agreeing if o.get('icon_id'))
                        if icons:
                            icon,n=icons.most_common(1)[0]
                            if n>=3 and n/sum(icons.values())>=.70:
                                row.update(icon_id=icon,node_status='known')
                            elif len(icons)>1:row['node_status']='conflict'
                elif conflict:row.update(occupant_status='conflict',node_status='conflict')
                pair_ids = {id(x) for x in positive_pair}
                exported = positive_pair + [x for x in sorted(pool,key=lambda x:x['confidence'],reverse=True)
                                            if id(x) not in pair_ids]
                row['evidence']=[dict(x,frame_path=f"frames/{x['frame_id']:04d}.png") for x in exported[:6]]
            cells.append(row)
        # A strongly established unique player/singularity can rule out a weak
        # alternative ONLY when that alternative also has multiple clear empty
        # views. Preserve the contradictory evidence and explain the constraint.
        for kind in ('player','singularity'):
            established=[c for c in cells if c['occupant']==kind and c['occupant_status']=='confirmed'
                         and c.get('occupant_evidence_counts',{}).get(kind,0)>=6
                         and _independent_positive_witness(
                             self.evidence[FACES.index(c['face'])*9+c['row']*3+c['col']],
                             kind, self.identity_corrections, required=6)]
            if len(established)!=1:continue
            anchor=established[0]
            strength=anchor['occupant_evidence_scores'].get(kind,0.)
            if strength/max(1e-6,sum(anchor['occupant_evidence_scores'].values()))<.90:continue
            for i,cell in enumerate(cells):
                counts=cell.get('occupant_evidence_counts',{})
                scores=cell.get('occupant_evidence_scores',{})
                if cell is anchor or cell['occupant_status']!='conflict':continue
                if not(0<counts.get(kind,0)<=2 and counts.get('none',0)>=5):continue
                if any(k not in ('none',kind) for k in counts) or strength<4*scores.get(kind,0):continue
                negatives=[o for o in self.evidence[i] if o['occupant']=='none']
                icons=Counter(o['icon_id'] for o in negatives if o.get('icon_id'))
                if not icons:continue
                icon,number=icons.most_common(1)[0]
                if number/len(negatives)<.8:continue
                cell.update(occupant='none',occupant_status='confirmed',icon_id=icon,node_status='known',
                    confidence=round(sum(o['confidence'] for o in negatives)/len(negatives),3),
                    constraint_resolution=dict(kind=kind,confirmed_at={k:anchor[k] for k in ('face','row','col')},
                        reason='unique_target_and_multiple_clear_negative_views'))
        targets={k:[c for c in cells if c['occupant']==k and c['occupant_status']=='confirmed']
                 for k in ('player','singularity','inspiration')}
        for kind in ('player','singularity'):
            if len(targets[kind])>1:
                for c in targets[kind]:c['occupant_status']='conflict'
        known=sum(c['occupant_status']=='confirmed' and c['node_status'] in ('known','not_required_target') for c in cells)
        faces=sum(any(c['face']==f and self.evidence[i] for i,c in enumerate(cells)) for f in FACES)
        coordinate=lambda c:{key:c[key] for key in ('face','row','col')}
        return dict(schema='resonance_pc.deep_dive_layout.v1',coordinate_frame='scan_local',
            map_revision=self.identity_corrections,
            status='completed' if known==54 and len(targets['player'])==len(targets['singularity'])==1 else 'partial',
            cells=cells,faces=[dict(face_id=f,normal=BASES[f][0],column_axis=BASES[f][1],row_axis=BASES[f][2]) for f in FACES],
            known_cells=known,faces_observed=faces,
            layout_complete=known==54 and len(targets['player'])==len(targets['singularity'])==1,
            semantic_mapping_complete=False,
            player_cell=coordinate(targets['player'][0]) if len(targets['player'])==1 else None,
            singularity_cell=coordinate(targets['singularity'][0]) if len(targets['singularity'])==1 else None,
            inspiration_cells=[coordinate(c) for c in targets['inspiration']],
            diagnostics=dict(frames_seen=self.frames_seen,tracking_failures=self.tracking_failures,
                last_error=self.last_error,view_groups=len(self.view_rotations),geometry='recording_20260927_reset_seed_v1',
                identity_corrections=self.identity_corrections,
                glyph_anchor_age_sec=time.monotonic()-(self.glyph_anchor_at or 0.),
                glyph_anchor_reason=self.glyph_anchor_reason,
                geometry_note='Experimental 1280x720 reset-view calibration; human comparison required',
                response_axes={str(k):v.tolist() for k,v in self.response_axes.items()}),
            pose=dict(rvec=self.rvec.ravel().tolist(),tvec=self.tvec.ravel().tolist(),camera_matrix=K.tolist()),
            target_clues=deepcopy(self.last_targets),
            target_candidate_associations=self.target_mask_observation())

    def annotate(self,image_rgb,*,fused_result=None):
        """Draw an explicit same-source result, or fuse afresh by default."""
        image=image_rgb.copy();result=self.result() if fused_result is None else fused_result
        for item in self.last_projected:
            cell=result['cells'][item['index']]
            color=(60,230,140) if cell['occupant_status']=='confirmed' else (255,190,50)
            cv2.polylines(image,[np.int32(item['quad'])],True,color,1,cv2.LINE_AA)
            centre=np.int32(item['centre'])
            text=f"{cell['face']}{cell['row']}{cell['col']} {cell['occupant']}"
            cv2.putText(image,text,tuple(centre),cv2.FONT_HERSHEY_SIMPLEX,.33,color,1,cv2.LINE_AA)
        for target in self.last_targets:
            x,y,w,h=map(int,target['box'])
            cv2.rectangle(image,(x,y),(x+w,y+h),(255,230,20),1)
            cv2.putText(image,target['kind'],(x,y-4),0,.4,(255,230,20),1)
        cv2.putText(image,f"atlas {result['known_cells']}/54 | scan_local | experimental",(310,70),0,.55,(255,255,255),1)
        return image


__all__=['LayoutScanner']
