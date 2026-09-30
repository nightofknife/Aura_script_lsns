"""Check DP reported statistics by direct stochastic trajectories.

Does not call the DP's movement, rotation, chance or scoring functions. Uses
its chosen policies and symmetry lookup as inputs to independently evaluate.
"""
import itertools
import json
from pathlib import Path
import numpy as np
from numba import njit

ROOT=Path(__file__).resolve().parent
B=np.array([[(0,-1,0),(1,0,0),(0,0,-1)],
            [(1,0,0),(0,0,1),(0,1,0)],
            [(0,0,-1),(1,0,0),(0,1,0)],
            [(0,1,0),(1,0,0),(0,0,1)],
            [(-1,0,0),(0,0,-1),(0,1,0)],
            [(0,0,1),(-1,0,0),(0,1,0)]],dtype=np.int64)
points=np.array([n+(c-1)*u+(r-1)*v for n,u,v in B for r in range(3) for c in range(3)])
normals=np.repeat(B[:,0],9,axis=0)
inverse={tuple(p)+tuple(n):i for i,(p,n) in enumerate(zip(points,normals))}
def rotate_vector(v,axis,sign):
    x,y,z=map(int,v)
    if axis==0:return (x,-sign*z,sign*y)
    if axis==1:return (sign*z,y,-sign*x)
    return (-sign*y,sign*x,z)
rotate=np.tile(np.arange(55),(54,4,1))
for a in range(54):
    for tangent_index in range(2):
        axis=int(np.flatnonzero(B[a//9,1+tangent_index])[0])
        for turn_index,sign in enumerate((-1,1)):
            for i in range(54):
                if points[i,axis]==points[a,axis]:
                    rotate[a,2*tangent_index+turn_index,i]=inverse[
                        rotate_vector(points[i],axis,sign)+rotate_vector(normals[i],axis,sign)]
moves=np.full((54,4),-1,dtype=np.int64);counts=np.zeros(54,dtype=np.int64)
for i in range(54):
    f,x=divmod(i,9);r,c=divmod(x,3)
    for dr,dc in ((-1,0),(1,0),(0,-1),(0,1)):
        if 0<=r+dr<3 and 0<=c+dc<3:
            moves[i,counts[i]]=f*9+(r+dr)*3+c+dc;counts[i]+=1

@njit(cache=True)
def lookup_key(p,b,i,j):
    if i>j:i,j=j,i
    return ((p*55+b)*55+i)*55+j

@njit(cache=True)
def trajectory(policy,states,lookup,roots,moves,counts,rotate,samples,seed):
    np.random.seed(seed)
    rows=np.zeros((samples,5),dtype=np.int8)
    horizon=len(policy)
    for trial in range(samples):
        state=roots[np.random.randint(len(roots))]
        collected=0;consumed=0;meeting=False;turns=0
        for remaining in range(horizon,0,-1):
            p,b,i,j=states[state]
            action=policy[remaining-1,collected,state]
            assert action>=0
            ordinal=0;found=False
            for k in range(counts[p]):
                target=moves[p,k]
                if target==b:
                    if ordinal==action:
                        meeting=True;found=True
                    ordinal+=1
                else:
                    for d in range(4):
                        if ordinal==action:
                            if i==target:i=54;collected+=1
                            if j==target:j=54;collected+=1
                            perm=rotate[target,d]
                            p=perm[target];b=perm[b];i=perm[i];j=perm[j]
                            found=True
                        ordinal+=1
                        if found:break
                if found:break
            if not found:
                # These are the same action enumeration contract as the
                # policy file, but the actual transition is recomputed here.
                original_p=p;original_b=b;original_i=i;original_j=j
                for d in range(4):
                    perm=rotate[original_p,d]
                    p2=perm[original_p];b2=perm[original_b]
                    for k in range(counts[p2]):
                        target=moves[p2,k]
                        if ordinal==action:
                            p=target;b=b2;i=perm[original_i];j=perm[original_j]
                            if i==p:i=54;collected+=1
                            if j==p:j=54;collected+=1
                            meeting=(p==b);found=True
                        ordinal+=1
                        if found:break
                    if found:break
            assert found
            turns+=1
            if meeting:break
            candidates=np.empty(4,dtype=np.int64);length=0
            distance=abs((p%9)//3-(b%9)//3)+abs(p%3-b%3)
            for k in range(counts[b]):
                target=moves[b,k]
                delta=abs((p%9)//3-(target%9)//3)+abs(p%3-target%3)
                if p//9!=b//9 or delta==distance-1:
                    candidates[length]=target;length+=1
            b=candidates[np.random.randint(length)]
            if b==p:meeting=True;break
            if i==b:i=54;consumed+=1
            if j==b:j=54;consumed+=1
            perm=rotate[b,np.random.randint(4)]
            p=perm[p];b=perm[b];i=perm[i];j=perm[j]
            state=lookup[lookup_key(p,b,i,j)]
            assert state>=0
        rows[trial]=np.array((collected,consumed,int(meeting),turns,
                             int(meeting and collected==2)),dtype=np.int8)
    return rows

def main():
    data=np.load(ROOT/'transitions.npz')
    output={}
    references=json.loads((ROOT/'results_six.json').read_text())
    names={0:'deadline_first',1:'all_inspirations_first',2:'successful_inspiration_yield'}
    for mode,name in names.items():
        npz=np.load(ROOT/('mode_1_six.npz' if mode==1 else f'mode_{mode}.npz'))
        rows=trajectory(npz['policy'],data['states'],data['lookup'],npz['roots'],
                        moves,counts,rotate,100000,310030+mode)
        columns={'mean_collected':rows[:,0].astype(float),
                 'mean_boss_consumed':rows[:,1].astype(float),
                 'meet_probability':rows[:,2].astype(float),
                 'all_two_then_meet_probability':rows[:,4].astype(float),
                 'mean_success_weighted_collected':rows[:,0]*rows[:,2]}
        summary={}
        for label,values in columns.items():
            expected=references['modes'][name][-1][label]
            observed=float(values.mean());se=float(values.std()/np.sqrt(len(values)))
            summary[label]={'dp':expected,'rollout':observed,'standard_error':se}
            assert abs(observed-expected)<6*se+0.00001,(name,label,summary[label])
        summary['samples']=len(rows)
        summary['p_any_consumed']=float(np.mean(rows[:,1]>0))
        output[name]=summary
        print(name,json.dumps(summary),flush=True)
    (ROOT/'rollout_check.json').write_text(json.dumps(output,indent=2),encoding='utf-8')

if __name__=='__main__':main()
