"""Finite horizon exact stochastic cube model; analytical, not game automation.

Uniform two distinct inspiration slots among 52 regular slots is an explicit
model assumption. Boss-consumed inspirations cannot be credited to the player.
No item effects, node event effects, battle rewards or extra actions are modeled.
"""
from __future__ import annotations

import argparse
import itertools
import json
import time
from pathlib import Path

import numpy as np
from numba import njit

ROOT = Path(__file__).resolve().parent
BASES = np.array([
    [(0,-1,0),(1,0,0),(0,0,-1)],
    [(1,0,0),(0,0,1),(0,1,0)],
    [(0,0,-1),(1,0,0),(0,1,0)],
    [(0,1,0),(1,0,0),(0,0,1)],
    [(-1,0,0),(0,0,-1),(0,1,0)],
    [(0,0,1),(-1,0,0),(0,1,0)],
], dtype=np.int64)


def geometry():
    points = np.array([n+u*(c-1)+v*(r-1)
                       for n,u,v in BASES for r in range(3) for c in range(3)])
    normals = np.repeat(BASES[:,0],9,axis=0)
    inverse = {tuple(p)+tuple(n):i for i,(p,n) in enumerate(zip(points,normals))}
    permutations=[]
    rotation_ids={}
    for axis in range(3):
        unit=np.eye(3,dtype=np.int64)[axis]
        for layer in (-1,0,1):
            for sign in (-1,1):
                row=list(range(55))
                for i,(p,n) in enumerate(zip(points,normals)):
                    if p[axis]==layer:
                        p2=sign*np.cross(unit,p)+unit*(unit@p)
                        n2=sign*np.cross(unit,n)+unit*(unit@n)
                        row[i]=inverse[tuple(p2)+tuple(n2)]
                rotation_ids[axis,layer,sign]=len(permutations)
                permutations.append(row)
    rotations=np.array(permutations,dtype=np.int64)
    actor=np.zeros((54,4),dtype=np.int64)
    for i in range(54):
        for k,tangent in enumerate(BASES[i//9,1:]):
            axis=int(np.flatnonzero(tangent)[0])
            # Both signs are available, so physical axis sign has no effect
            # on the unordered action set.
            for j,sign in enumerate((-1,1)):
                actor[i,2*k+j]=rotation_ids[axis,int(points[i,axis]),sign]
    group=[]
    for permutation in itertools.permutations(range(3)):
        for signs in itertools.product((-1,1),repeat=3):
            matrix=np.zeros((3,3),dtype=np.int64)
            for i,j in enumerate(permutation):matrix[i,j]=signs[i]
            if round(np.linalg.det(matrix))!=1:continue
            group.append([inverse[tuple(matrix@p)+tuple(matrix@n)]
                          for p,n in zip(points,normals)]+[54])
    group=np.array(group,dtype=np.int64)
    moves=np.full((54,4),-1,dtype=np.int64)
    counts=np.zeros(54,dtype=np.int64)
    for i in range(54):
        f,rem=divmod(i,9);r,c=divmod(rem,3)
        for dr,dc in ((-1,0),(1,0),(0,-1),(0,1)):
            if 0<=r+dr<3 and 0<=c+dc<3:
                moves[i,counts[i]]=f*9+(r+dr)*3+c+dc
                counts[i]+=1
    for row in rotations:
        assert sorted(row[:54])==list(range(54))
        assert np.array_equal(row[row[row[row]]],np.arange(55))
    assert group.shape==(24,55)
    return rotations,actor,group,moves,counts


@njit(cache=True)
def key(p,b,i,j):
    return ((p*55+b)*55+i)*55+j


@njit(cache=True)
def canonical(p,b,i,j,group):
    result=55**4
    for g in range(24):
        pi=group[g,i];pj=group[g,j]
        if pi>pj:pi,pj=pj,pi
        candidate=key(group[g,p],group[g,b],pi,pj)
        if candidate<result:result=candidate
    return result


@njit(cache=True)
def state_pool(group):
    states=np.zeros((240000,4),dtype=np.int16)
    count=0
    for p in (0,1,4):
        for b in range(54):
            if b==p:continue
            for i in range(55):
                if i==p or i==b:continue
                for j in range(i+1,55):
                    if j==p or j==b:continue
                    if canonical(p,b,i,j,group)==key(p,b,i,j):
                        states[count]=np.array((p,b,i,j),dtype=np.int16)
                        count+=1
            if canonical(p,b,54,54,group)==key(p,b,54,54):
                states[count]=np.array((p,b,54,54),dtype=np.int16)
                count+=1
    states=states[:count]
    lookup=np.full(55**4,-1,dtype=np.int32)
    for s in range(count):
        p,b,i,j=states[s]
        for g in range(24):
            pi=group[g,i];pj=group[g,j]
            if pi>pj:pi,pj=pj,pi
            lookup[key(group[g,p],group[g,b],pi,pj)]=s
    return states,lookup


@njit(cache=True)
def moved_items(i,j,permutation,removed):
    gain=0
    if i==removed:i=54;gain+=1
    if j==removed:j=54;gain+=1
    i2=permutation[i];j2=permutation[j]
    if i2>j2:i2,j2=j2,i2
    return i2,j2,gain


@njit(cache=True)
def manhattan(p,b):
    return abs((p%9)//3-(b%9)//3)+abs(p%3-b%3)


@njit(cache=True)
def transitions(states,lookup,rotations,actor,moves,counts):
    length=len(states)
    a_index=np.full((length,32),-1,dtype=np.int32)
    a_gain=np.zeros((length,32),dtype=np.int8)
    a_count=np.zeros(length,dtype=np.int8)
    b_index=np.full((length,16),-1,dtype=np.int32)
    b_eat=np.zeros((length,16),dtype=np.int8)
    b_weight=np.zeros((length,16),dtype=np.float64)
    b_count=np.zeros(length,dtype=np.int8)
    for s in range(length):
        p,b,i,j=states[s]
        # Move first.
        for k in range(counts[p]):
            m=moves[p,k]
            if m==b:
                a_index[s,a_count[s]]=-2
                a_count[s]+=1
                continue
            for d in range(4):
                perm=rotations[actor[m,d]]
                i2,j2,gain=moved_items(i,j,perm,m)
                t=lookup[key(perm[m],perm[b],i2,j2)]
                assert t>=0
                a_index[s,a_count[s]]=t
                a_gain[s,a_count[s]]=gain
                a_count[s]+=1
        # Rotate first.
        for d in range(4):
            perm=rotations[actor[p,d]]
            p2=perm[p];b2=perm[b]
            for k in range(counts[p2]):
                m=moves[p2,k]
                gain=int(perm[i]==m)+int(perm[j]==m)
                if m==b2:
                    a_index[s,a_count[s]]=-2
                    a_count[s]+=1
                    continue
                i2=54 if perm[i]==m else perm[i]
                j2=54 if perm[j]==m else perm[j]
                if i2>j2:i2,j2=j2,i2
                t=lookup[key(m,b2,i2,j2)]
                assert t>=0
                a_index[s,a_count[s]]=t
                a_gain[s,a_count[s]]=gain
                a_count[s]+=1
        allowed=0
        distance=manhattan(p,b)
        for k in range(counts[b]):
            m=moves[b,k]
            if p//9!=b//9 or manhattan(p,m)==distance-1:allowed+=1
        assert allowed>0
        for k in range(counts[b]):
            m=moves[b,k]
            if p//9==b//9 and manhattan(p,m)!=distance-1:continue
            if m==p:
                b_index[s,b_count[s]]=-2
                b_weight[s,b_count[s]]=1./allowed
                b_count[s]+=1
            else:
                for d in range(4):
                    perm=rotations[actor[m,d]]
                    i2,j2,eat=moved_items(i,j,perm,m)
                    t=lookup[key(perm[p],perm[m],i2,j2)]
                    assert t>=0
                    b_index[s,b_count[s]]=t
                    b_eat[s,b_count[s]]=eat
                    b_weight[s,b_count[s]]=1./(allowed*4)
                    b_count[s]+=1
        assert abs(b_weight[s].sum()-1.)<1e-12
    return a_index,a_gain,a_count,b_index,b_eat,b_weight,b_count


# Stat columns: meet probability, all-2 probability, future collected,
# future collected squared, future consumed, meeting first moment,
# all-2 first moment, successful total collected (failed deadline counts zero).
@njit(cache=True)
def solve_step(prev,states,trans,mode):
    ai,ag,ac,bi,be,bw,bc=trans
    n=len(states)
    boss=np.zeros_like(prev)
    current=np.zeros_like(prev)
    policy=np.full((3,n),-1,dtype=np.int8)
    for c in range(3):
        for s in range(n):
            free=int(states[s,2]!=54)+int(states[s,3]!=54)
            if c+free>2:continue
            for k in range(bc[s]):
                t=bi[s,k];w=bw[s,k]
                if t==-2:
                    boss[c,s,0]+=w
                    boss[c,s,1]+=w*int(c==2)
                    boss[c,s,5]+=w
                    boss[c,s,6]+=w*int(c==2)
                    boss[c,s,7]+=w*c
                else:
                    for stat in range(8):boss[c,s,stat]+=w*prev[c,t,stat]
                    boss[c,s,4]+=w*be[s,k]
                    boss[c,s,5]+=w*prev[c,t,0]
                    boss[c,s,6]+=w*prev[c,t,1]
    for c in range(3):
        for s in range(n):
            free=int(states[s,2]!=54)+int(states[s,3]!=54)
            if c+free>2:continue
            selected=-1
            best=np.zeros(8,dtype=np.float64)
            for k in range(ac[s]):
                t=ai[s,k];gain=ag[s,k]
                candidate=np.zeros(8,dtype=np.float64)
                if t==-2:
                    candidate[0]=1.
                    candidate[1]=1. if c==2 else 0.
                    candidate[5]=1.
                    candidate[6]=1. if c==2 else 0.
                    candidate[7]=float(c)
                else:
                    candidate[:]=boss[c+gain,t]
                    candidate[2]+=gain
                    candidate[3]+=gain*gain+2*gain*boss[c+gain,t,2]
                # Stable lexicographic ranking. The two all-inspiration
                # columns require c==2, so boss-consumed items never qualify.
                order=np.array((0,2,-5),dtype=np.int64)
                if mode==1:order=np.array((1,-6,0,2),dtype=np.int64)
                if mode==2:order=np.array((7,0,2,-5),dtype=np.int64)
                if mode==3:order=np.array((2,0,-5),dtype=np.int64)
                better=selected<0
                if not better:
                    for index in order:
                        sign=1. if index>=0 else -1.
                        column=abs(index)
                        difference=sign*(candidate[column]-best[column])
                        if difference>1e-12:better=True;break
                        if difference< -1e-12:break
                if better:
                    best=candidate
                    selected=k
            current[c,s]=best
            policy[c,s]=selected
    return current,policy


def summary(table,roots):
    rows=table[0,roots]
    mean=rows.mean(axis=0)
    mean_collected=float(mean[2])
    p2=float((mean[3]-mean[2])/2)
    p1=float(2*mean[2]-mean[3])
    p0=float(1-p1-p2)
    return {
        'meet_probability':float(mean[0]),
        'all_two_then_meet_probability':float(mean[1]),
        'mean_collected':mean_collected,
        'mean_boss_consumed':float(mean[4]),
        'collected_distribution':[p0,p1,p2],
        'mean_rounds_given_meeting':float(mean[5]/mean[0]) if mean[0] else None,
        'mean_rounds_given_all_two_and_meeting':float(mean[6]/mean[1]) if mean[1] else None,
        'mean_success_weighted_collected':float(mean[7]),
        'mean_collected_given_meeting':float(mean[7]/mean[0]) if mean[0] else None,
        'layout_mean_collected_range':[float(rows[:,2].min()),float(rows[:,2].max())],
        'layout_meet_probability_range':[float(rows[:,0].min()),float(rows[:,0].max())],
    }


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--horizon',type=int,default=6)
    ap.add_argument('--modes',default='0,1,2')
    args=ap.parse_args()
    started=time.monotonic()
    cache=ROOT/'transitions.npz'
    if cache.exists():
        saved=np.load(cache)
        states=saved['states'];lookup=saved['lookup']
        trans=tuple(saved[x] for x in ('ai','ag','ac','bi','be','bw','bc'))
    else:
        rotations,actor,group,moves,counts=geometry()
        states,lookup=state_pool(group)
        print('state_pool',len(states),flush=True)
        trans=transitions(states,lookup,rotations,actor,moves,counts)
        np.savez(cache,states=states,lookup=lookup,ai=trans[0],ag=trans[1],ac=trans[2],
                 bi=trans[3],be=trans[4],bw=trans[5],bc=trans[6])
    p,b=4,31
    slots=[i for i in range(54) if i not in (p,b)]
    pairs=np.array(list(itertools.combinations(slots,2)))
    roots=np.array([lookup[key(p,b,int(i),int(j))] for i,j in pairs])
    assert len(roots)==1326 and np.all(roots>=0)
    empty_root=lookup[key(p,b,54,54)]
    result={'assumptions':{
        'inspiration_spawn':'uniform unordered pair among 52 non-role slots',
        'boss':'uniform admissible neighbour; uniform four layer rotations',
        'destroyed_inspiration':'not player collected; no recovery credited',
        'items_events':'no extra actions or movement effects',
        'initial_positions':{'player':['U',1,1],'boss':['D',1,1]},
    },'layout_count':1326,'canonical_state_count':int(len(states)),'modes':{}}
    names={0:'deadline_first',1:'all_inspirations_first',2:'successful_inspiration_yield',
           3:'collection_only_upper_comparison'}
    for mode in [int(x) for x in args.modes.split(',')]:
        table=np.zeros((3,len(states),8))
        history=[];policies=[]
        for horizon in range(1,args.horizon+1):
            table,policy=solve_step(table,states,trans,mode)
            row=summary(table,roots)
            row['horizon']=horizon
            row['no_inspiration_meet_probability']=float(table[0,empty_root,0])
            history.append(row);policies.append(policy)
            print(names[mode],json.dumps(row),flush=True)
        result['modes'][names[mode]]=history
        np.savez(ROOT/f'mode_{mode}.npz',table=table,policy=np.array(policies),
                 roots=roots,pairs=pairs)
        (ROOT/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    result['elapsed_sec']=time.monotonic()-started
    (ROOT/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('elapsed_sec',result['elapsed_sec'],flush=True)


if __name__=='__main__':main()
