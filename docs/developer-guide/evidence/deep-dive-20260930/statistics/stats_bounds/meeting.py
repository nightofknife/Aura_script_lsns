import itertools, json, math
from collections import defaultdict
from pathlib import Path
import numpy as np

OUT=Path(__file__).parent
BASES={
 'U':((0,-1,0),(1,0,0),(0,0,-1)),
 'R':((1,0,0),(0,0,1),(0,1,0)),
 'F':((0,0,-1),(1,0,0),(0,1,0)),
 'D':((0,1,0),(1,0,0),(0,0,1)),
 'L':((-1,0,0),(0,0,-1),(0,1,0)),
 'B':((0,0,1),(-1,0,0),(0,1,0))}
FACES=list(BASES)
cells=[(f,r,c) for f in FACES for r in range(3) for c in range(3)]
idx={x:i for i,x in enumerate(cells)}
points=[]; norms=[]
for f,r,c in cells:
 n,u,v=map(np.array,BASES[f]); points.append(n+(c-1)*u+(r-1)*v); norms.append(n)
points=np.array(points);norms=np.array(norms)
lookup={(tuple(p),tuple(n)):i for i,(p,n) in enumerate(zip(points,norms))}
neigh=[[idx[(f,r+dr,c+dc)] for dr,dc in ((1,0),(-1,0),(0,1),(0,-1)) if 0<=r+dr<3 and 0<=c+dc<3] for f,r,c in cells]
rot=[]
for i,(f,r,c) in enumerate(cells):
 local=[]
 for a in (BASES[f][1],BASES[f][2]):
  axis=np.array(a);lev=points[i]@axis
  for sign in (-1,1):
   def quarter(x):return axis*(axis@x)+sign*np.cross(axis,x)
   perm=[]
   for j in range(54):
    p,n=points[j],norms[j]
    if p@axis==lev:p,n=quarter(p),quarter(n)
    perm.append(lookup[(tuple(p),tuple(n))])
   assert len(set(perm))==54
   local.append(perm)
 rot.append(local)

sym=[]
for axes in itertools.permutations(range(3)):
 for signs in itertools.product((-1,1),repeat=3):
  m=np.zeros((3,3),int)
  for row,col in enumerate(axes):m[row,col]=signs[row]
  if round(np.linalg.det(m))!=1:continue
  sym.append([lookup[(tuple(m@points[j]),tuple(m@norms[j]))] for j in range(54)])
canon={(p,b):min((s[p],s[b]) for s in sym) for p in range(54) for b in range(54) if p!=b}
states=sorted(set(canon.values()));sid={x:i for i,x in enumerate(states)}
def ci(p,b):return sid[canon[(p,b)]]

def player_results(p,b):
 # Each player action consumes a move and rotation unless move encounters boss.
 result=set()
 for pm in neigh[p]:
  if pm==b:result.add(None);continue
  for perm in rot[pm]:result.add((perm[pm],perm[b]))
 for perm in rot[p]:
  pr,br=perm[p],perm[b]
  for pm in neigh[pr]:result.add(None if pm==br else (pm,br))
 return result

def boss_result(p,b):
 dest=neigh[b]
 fp,rp,cp=cells[p];fb,rb,cb=cells[b]
 if fp==fb:
  dist=abs(rp-rb)+abs(cp-cb)
  dest=[bm for bm in dest if abs(cells[bm][1]-rp)+abs(cells[bm][2]-cp)==dist-1]
 mass=defaultdict(float)
 for bm in dest:
  if bm==p:mass[-1]+=1/len(dest);continue
  for perm in rot[bm]:mass[ci(perm[p],perm[bm])]+=1/(4*len(dest))
 assert abs(sum(mass.values())-1)<1e-12
 return tuple(sorted(mass.items()))
actions=[]
for p,b in states:
 acts=set()
 for result in player_results(p,b):
  acts.add(((-1,1.),) if result is None else boss_result(*result))
 actions.append(sorted(acts))
N=len(states)
maxA=max(map(len,actions));nexts=np.full((N,maxA,16),-1,int);probs=np.zeros((N,maxA,16))
legal=np.zeros((N,maxA),bool)
for s,acts in enumerate(actions):
 for a,t in enumerate(acts):
  legal[s,a]=True
  for k,(j,w) in enumerate(t):nexts[s,a,k]=j;probs[s,a,k]=w
def expect(v):
 extended=np.r_[v,0.]
 return np.where(legal,1+(extended[nexts]*probs).sum(axis=2),np.inf)
def worst(v):
 extended=np.r_[v,0.]
 return np.where(legal,1+np.where(probs>0,extended[nexts],0).max(axis=2),np.inf)
g=np.full(N,np.inf)
for k in range(1,30):
 ng=worst(g).min(axis=1)
 if np.array_equal(g,ng):break
 g=ng
 print('guaranteed',k, dict(zip(*np.unique(g,return_counts=True))),flush=True)
start=ci(idx[('U',1,1)],idx[('D',1,1)])
lower=np.zeros(N)
for step in range(10000):
 lower=expect(lower).min(axis=1)
 if step%10==0:
  pi=expect(lower).argmin(axis=1)
  matrix=np.eye(N)
  for s in range(N):
   for j,w in actions[s][pi[s]]:
    if j>=0:matrix[s,j]-=w
  upper=np.linalg.solve(matrix,np.ones(N))
  gap=float((upper-lower).max())
  if gap<1e-12:break
print('N',N,'mean',upper[start],'bound',g[start],'gap',gap,'step',step,flush=True)
pi=expect(upper).argmin(axis=1)
policy_g=np.full(N,np.inf)
for k in range(1,30):
 ng=worst(policy_g)[np.arange(N),pi]
 if np.array_equal(ng,policy_g):break
 policy_g=ng
result={'state_count':N,'all_ordered_pairs':54*53,'symmetry_count':len(sym),'start':cells[states[start][0]],'boss':cells[states[start][1]],'guaranteed_minimum_turns':int(g[start]) if np.isfinite(g[start]) else None,'optimal_expected_turns':float(upper[start]),'expectation_max_error_bound':gap,'finite_horizon_lower_bound':float(lower[start]),'value_iteration_depth':step+1,'expected_policy_worst_case_turns':int(policy_g[start]) if np.isfinite(policy_g[start]) else None,'all_state_guaranteed_max':float(g.max()) if np.isfinite(g).all() else None,'expected_policy_all_state_guaranteed_max':float(policy_g.max()) if np.isfinite(policy_g).all() else None,'rank_counts':{str(float(x)):int(n) for x,n in zip(*np.unique(g,return_counts=True))}}
print(json.dumps(result,indent=2))
(OUT/'meeting.json').write_text(json.dumps(result,indent=2),encoding='utf-8')

# Exact finite support distribution when expected-optimal tie-breaking also guarantees.
mass=np.zeros(N);mass[start]=1
distribution=[]
for t in range(1,100):
 new=np.zeros(N);ended=0.
 for s,prob in enumerate(mass):
  if prob==0:continue
  for j,w in actions[s][pi[s]]:
   if j==-1:ended+=prob*w
   else:new[j]+=prob*w
 distribution.append({'turn':t,'probability':ended,'remaining':float(new.sum())})
 mass=new
 if mass.sum()<1e-14:break
result['encounter_turn_distribution']=distribution
# A rational policy evaluation plus all-action Bellman inequalities certifies optimality.
import sympy as sp
from fractions import Fraction
def rat(w):
 f=Fraction(w).limit_denominator(48);return sp.Rational(f.numerator,f.denominator)
Q=sp.eye(N)
for s in range(N):
 for j,w in actions[s][pi[s]]:
  if j>=0:Q[s,j]-=rat(w)
exact=Q.inv()*sp.ones(N,1)
assert all(min(1+sum(rat(w)*exact[j] for j,w in a if j>=0) for a in acts)==exact[s] for s,acts in enumerate(actions))
result['optimal_expected_turns_exact']=str(exact[start])
result['exact_all_action_bellman_verified']=True
print('exact',exact[start],flush=True)

# Independently enumerate all 2862 physical states, to check 24-rotation quotient.
def rational_action(t):return tuple((j,rat(w)) for j,w in t)
for p,b in canon:
 actual=set()
 for r in player_results(p,b):actual.add(rational_action(((-1,1.),) if r is None else boss_result(*r)))
 assert actual=={rational_action(a) for a in actions[ci(p,b)]}
result['all_2862_quotient_actions_verified']=True
complement=set(np.where(~np.isfinite(g))[0])
assert all(all(any(j in complement for j,w in a if w>0) for a in actions[s]) for s in complement)
result['non_guaranteed_closed_complement_verified']=True

prob=[sp.Rational(0)]*N
deadline=[]
for t in range(1,7):
 prob=[max(sum(rat(w)*(1 if j==-1 else prob[j]) for j,w in a) for a in acts) for acts in actions]
 deadline.append({'turn_budget':t,'best_success_probability_exact':str(prob[start]),'best_success_probability':float(prob[start])})
result['optimal_deadline_encounter_probability']=deadline
print('deadline',deadline,flush=True)

def described_player(p,b):
 for pm in neigh[p]:
  if pm==b:yield None,{'order':'move','destination':cells[pm]};continue
  for a,perm in enumerate(rot[pm]):
   yield (perm[pm],perm[b]),{'order':'move_then_rotate','destination':cells[pm],'rotation_actor':cells[pm],'tangent_axis_index':a//2,'sign':(-1,1)[a%2]}
 for a,perm in enumerate(rot[p]):
  pr,br=perm[p],perm[b]
  for pm in neigh[pr]:
   yield None if pm==br else (pm,br),{'order':'rotate_then_move','rotation_actor':cells[p],'tangent_axis_index':a//2,'sign':(-1,1)[a%2],'destination':cells[pm]}

seen={};cycle=[];s=start
while s not in seen:
 seen[s]=len(cycle)
 p,b=states[s];target=actions[s][pi[s]]
 after,description=next((r,d) for r,d in described_player(p,b) if r is not None and rational_action(boss_result(*r))==rational_action(target))
 pp,bb=after
 destinations=neigh[bb]
 if cells[pp][0]==cells[bb][0]:
  dist=sum(abs(cells[pp][k]-cells[bb][k]) for k in (1,2))
  destinations=[bm for bm in destinations if sum(abs(cells[pp][k]-cells[bm][k]) for k in (1,2))==dist-1]
 adverse=next((bm,a,perm) for bm in destinations if bm!=pp for a,perm in enumerate(rot[bm]) if ci(perm[pp],perm[bm]) in complement)
 bm,a,perm=adverse;ns=ci(perm[pp],perm[bm])
 cycle.append({'canonical_state':s,'player':cells[p],'boss':cells[b],'player_action':description,'after_player':[cells[pp],cells[bb]],'boss_move':cells[bm],'boss_rotation_tangent_axis_index':a//2,'boss_rotation_sign':(-1,1)[a%2],'next_canonical_state':ns})
 s=ns
result['adversarial_cycle_under_fastest_expected_policy']={'prefix_and_cycle':cycle,'cycle_starts_at_index':seen[s],'note':'Each row normalized by a whole-cube rotation; this is a concrete positive-probability indefinitely extendable path, while closed-complement check handles every player policy.'}
(OUT/'meeting.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
