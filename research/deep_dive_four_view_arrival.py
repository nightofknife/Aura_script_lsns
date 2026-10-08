"""Pixel structure alignment research; no 3D pose, semantic or input authority.

Manual anchors only locate reference edges. Actual LSD segments calibrate their
subpixel baseline. Single-frame geometry never determines the ordered page.
"""
import math
import time
import cv2
import numpy as np


def _segments(rgb,roi=None):
    if not isinstance(rgb,np.ndarray) or rgb.shape!=(720,1280,3) or rgb.dtype!=np.uint8:
        raise ValueError('expected_1280x720_rgb')
    gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    x,y,w,h=(0,0,1280,720) if roi is None else roi
    found=cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD).detect(gray[y:y+h,x:x+w])[0]
    if found is None:return np.empty((0,4))
    return found.reshape(-1,4).astype(float)+np.array((x,y,x,y))


def _candidate_lines(segments, endpoints, radius, *, oriented=False):
    p,q=np.asarray(endpoints,float)
    tangent=q-p;length=float(np.linalg.norm(tangent));tangent/=length
    normal=np.array((-tangent[1],tangent[0]));center=(p+q)/2
    if not len(segments):return []
    a=segments[:,:2];b=segments[:,2:];vectors=b-a
    sizes=np.linalg.norm(vectors,axis=1)
    cosine=(vectors@tangent)/np.maximum(sizes,1e-9)
    along=np.column_stack(((a-p)@tangent,(b-p)@tangent))
    overlap=np.maximum(0.,np.minimum(length,along.max(axis=1))-np.maximum(0.,along.min(axis=1)))/length
    offsets=((a+b)/2-center)@normal
    good=(sizes>=max(25.,length*.40))&((cosine if oriented else abs(cosine))>=math.cos(math.radians(6.)))&(overlap>=.50)&(abs(offsets)<=radius)
    results=[]
    for i in np.flatnonzero(good):
        rotation=float(np.degrees(np.arctan2(vectors[i]@normal,vectors[i]@tangent)))
        if not oriented:rotation=(rotation+90)%180-90
        results.append(dict(endpoints=[a[i].tolist(),b[i].tolist()],offset_px=float(offsets[i]),
            angle_deg=rotation,overlap=float(overlap[i]),length_px=float(sizes[i]),
            evidence_score=float(overlap[i]*min(sizes[i]/length,1.))))
    return sorted(results,key=lambda x:-x['evidence_score'])


def _edge_descriptor(gray,endpoints):
    p,q=np.asarray(endpoints,float);u=q-p;u/=np.linalg.norm(u);n=np.array((-u[1],u[0]))
    centers=p+(q-p)*np.linspace(.15,.85,16)[:,None]
    points=centers[:,None]+n*np.arange(-7,8)[None,:,None]
    patch=cv2.remap(gray,points[:,:,0].astype(np.float32),points[:,:,1].astype(np.float32),cv2.INTER_LINEAR)
    # A normal profile describes the dark/bright boundary and parallel lip,
    # rather than the glyph inside a tile. Normalize shader brightness.
    profile=patch.mean(axis=0).astype(float);profile-=profile.mean()
    return profile/max(np.linalg.norm(profile),1e-9)


class ReferenceLineArrival:
    def __init__(self,rgb,anchors,*,search_radius_px=12.):
        if not 4<=search_radius_px<=32:raise ValueError('invalid_search_radius')
        self.search_radius=float(search_radius_px)
        self.baselines=[];self.calibration_rejected=[]
        corners=np.asarray([item['endpoints'] for item in anchors],float).reshape(-1,2)
        low=np.maximum(0,np.floor(corners.min(axis=0)-search_radius_px-12)).astype(int)
        high=np.minimum((1280,720),np.ceil(corners.max(axis=0)+search_radius_px+12)).astype(int)
        self.roi=tuple(map(int,np.r_[low,high-low]))
        segments=_segments(rgb,self.roi)
        gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
        for item in anchors:
            points=np.asarray(item['endpoints'],float)
            if points.shape!=(2,2) or not np.isfinite(points).all() or np.linalg.norm(points[1]-points[0])<30:
                raise ValueError('invalid_anchor')
            candidates=_candidate_lines(segments,points,8.)
            if not candidates:
                self.calibration_rejected.append(item['id']);continue
            # Only on the manually identified reference: closest actual segment
            # selects which annotated boundary/polarity is meant, not arrival.
            best=min(candidates,key=lambda c:(abs(c['offset_px']),-c['evidence_score']))
            self.baselines.append(dict(id=item['id'],region=item['region'],
                manual_endpoints=points.tolist(),actual_reference=best,
                edge_descriptor=_edge_descriptor(gray,best['endpoints']).tolist()))

    def measure(self,rgb):
        begin=time.perf_counter();segments=_segments(rgb,self.roi);lines=[]
        gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
        for baseline in self.baselines:
            reference=baseline['actual_reference']['endpoints']
            candidates=_candidate_lines(segments,reference,self.search_radius,oriented=True)
            item=dict(id=baseline['id'],region=baseline['region'],reference=reference,
                alternatives=candidates,supported=False,ambiguous=False)
            for candidate in candidates:
                candidate['boundary_profile_similarity']=float(_edge_descriptor(gray,candidate['endpoints'])@np.asarray(baseline['edge_descriptor']))
            candidates.sort(key=lambda c:(-c['boundary_profile_similarity'],-c['evidence_score']))
            if candidates:
                strongest=candidates[0]
                viable=[c for c in candidates if c['boundary_profile_similarity']>=max(.70,strongest['boundary_profile_similarity']-.03)]
                item['ambiguous']=any(abs(c['offset_px']-strongest['offset_px'])>3. or
                    abs(c['angle_deg']-strongest['angle_deg'])>2. for c in viable)
                item.update(selected_actual=strongest,supported=bool(not item['ambiguous'] and strongest['boundary_profile_similarity']>=.70))
            lines.append(item)
        valid=[line for line in lines if line['supported']]
        offsets=np.asarray([line['selected_actual']['offset_px'] for line in valid])
        angles=np.asarray([line['selected_actual']['angle_deg'] for line in valid])
        regions=sorted({line['region'] for line in valid})
        translation=None;translation_condition=None
        if valid:
            normals=[]
            for line in valid:
                p,q=np.asarray(line['reference']);u=q-p;u/=np.linalg.norm(u)
                normals.append((-u[1],u[0]))
            normal_matrix=np.asarray(normals)
            singular=np.linalg.svd(normal_matrix,compute_uv=False)
            if len(singular)==2 and singular[-1]>1e-6:
                translation_condition=float(singular[0]/singular[-1])
                translation=np.linalg.lstsq(normal_matrix,offsets,rcond=None)[0]
        enough=len(valid)>=6 and len(regions)>=3 and translation_condition is not None and translation_condition<=20.
        median=float(np.median(abs(offsets))) if len(offsets) else None
        p90=float(np.quantile(abs(offsets),.9)) if len(offsets) else None
        a90=float(np.quantile(abs(angles),.9)) if len(angles) else None
        translation_norm=float(np.linalg.norm(translation)) if translation is not None else None
        match=bool(enough and median<=2. and p90<=4. and a90<=1.5 and translation_norm<=3.)
        departed=bool(enough and (median>5. or a90>3. or translation_norm>5.))
        return dict(status='geometry_match_candidate' if match else 'not_aligned' if enough else 'unknown',
            geometry_match_candidate=match,positive_departure_evidence=departed,
            supported_lines=len(valid),calibrated_lines=len(self.baselines),regions=regions,
            median_normal_offset_px=median,p90_normal_offset_px=p90,p90_angle_deg=a90,
            inferred_translation_px=translation.tolist() if translation is not None else None,
            translation_condition=translation_condition,
            signed_offsets={line['id']:line['selected_actual']['offset_px'] for line in valid},
            lines=lines,elapsed_sec=time.perf_counter()-begin,
            identity_from_geometry=False,arrival_confirmed=False,input_authorized=False,
            targets_ready=False,thresholds_calibrated_on_live_neighbourhood=False)


class OrderedArrivalGate:
    """Pure sequence gate. Caller owns reset, source binding and issued inputs.

    A missing edge is not positive departure. Repeated timestamps/ids and
    capture gaps revoke evidence. No page skip or direction is guessed.
    """
    def __init__(self,*,reset_confirmed,route_length=4):
        if reset_confirmed is not True:raise ValueError('confirmed_reset_required')
        if type(route_length) is not int or route_length<1:raise ValueError('invalid_route_length')
        self.target=0;self.length=route_length;self.previous=None
        self.left_previous=True;self.departures=0;self.matches=[];self.last=None
        self.await_release=False;self.release_frames=[]
        self.gap_blocked=False

    def observe(self,frame_id,frame_time,target_measurement,*,previous_measurement=None,released=False):
        if type(frame_id) is not int or frame_id<0 or type(frame_time) not in (int,float) or not math.isfinite(frame_time) or frame_time<0:
            raise ValueError('invalid_frame_source')
        current=(frame_id,float(frame_time))
        if self.gap_blocked:return dict(status='capture_gap_requires_reset',target=self.target)
        if self.last and current[1]-self.last[1]>1.:
            self.gap_blocked=True
            return dict(status='capture_gap_requires_reset',target=self.target)
        if self.last and (current[0]<=self.last[0] or current[1]<=self.last[1] or current[1]-self.last[1]>1.):
            self.matches=[];self.release_frames=[];self.await_release=False
            return dict(status='source_discontinuity_requires_reacquisition',target=self.target)
        self.last=current
        if self.target>=self.length:return dict(status='route_complete',target=self.target)
        if not self.left_previous:
            departed=previous_measurement and previous_measurement.get('positive_departure_evidence') is True
            self.departures=self.departures+1 if departed else 0
            if self.departures<2:return dict(status='waiting_actual_departure',target=self.target)
            self.left_previous=True
        match=target_measurement.get('geometry_match_candidate') is True
        if not match:
            self.matches=[];self.release_frames=[];self.await_release=False
            return dict(status='approach_or_unknown',target=self.target)
        if self.await_release:
            if not released:return dict(status='release_then_verify',target=self.target)
            self.release_frames.append(current)
            if len(self.release_frames)<2 or self.release_frames[-1][1]-self.release_frames[0][1]<.10:
                return dict(status='verifying_released_frames',target=self.target)
            completed=self.target;self.target+=1;self.previous=completed
            self.left_previous=False;self.departures=0;self.matches=[];self.release_frames=[];self.await_release=False
            return dict(status='ordered_arrival_confirmed',view_index=completed,target=self.target,
                targets_ready=False)
        self.matches.append(current)
        if len(self.matches)>=3 and self.matches[-1][1]-self.matches[0][1]>=.15:
            self.await_release=True
            return dict(status='release_then_verify',target=self.target)
        return dict(status='collecting_fresh_matching_frames',target=self.target)
