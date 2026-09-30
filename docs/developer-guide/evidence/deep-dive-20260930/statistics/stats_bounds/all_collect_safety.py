"""Upper certificates for collecting both inspirations before encounter."""
import itertools,json,time
from pathlib import Path
import numpy as np

ROOT=Path(__file__).parent
src=ROOT.parent/'stats_full'/'transitions.npz'
z=np.load(src)
states=z['states'];lookup=z['lookup'];ai=z['ai'];ag=z['ag'];ac=z['ac'];bi=z['bi'];be=z['be'];bw=z['bw'];bc=z['bc']
free=(states[:,2]!=54).astype(np.int8)+(states[:,3]!=54)
alive=np.ones(len(states),bool)
elimination=np.zeros(len(states),int)
steps=[]
start=time.monotonic()
for k in range(1,10000):
 b_good=np.where(bw>0,np.where(bi>=0,alive[np.maximum(bi,0)]&(be==0),(bi==-2)&(free[:,None]==0)),True).all(axis=1)
 p_good=np.where(ai>=0,b_good[np.maximum(ai,0)],(ai==-2)&(free[:,None]==0)).any(axis=1)
 nxt=alive&p_good
 newly=alive&~nxt;elimination[newly]=k
 steps.append({'iteration':k,'removed':int(newly.sum()),'remaining':int(nxt.sum())})
 print(steps[-1],flush=True)
 if np.array_equal(nxt,alive):break
 alive=nxt
def key(p,b,i,j):return ((p*55+b)*55+i)*55+j
slots=[i for i in range(54) if i not in (4,31)]
pairs=list(itertools.combinations(slots,2))
roots=np.array([lookup[key(4,31,i,j)] for i,j in pairs])
unsafe=[{'inspirations':[divmod(i,9),divmod(j,9)],'raw_slots':[i,j],'state_index':int(s),'elimination_iteration':int(elimination[s])} for (i,j),s in zip(pairs,roots) if not alive[s]]
result={'state_count':len(states),'safe_count':int(alive.sum()),'initial_layout_count':len(roots),'initial_unsafe_layouts':len(unsafe),'initial_safe_layouts':int(alive[roots].sum()),'iterations':steps,'unsafe_examples':unsafe[:20],'explanation':'Any almost-sure all-collection policy must stay forever within greatest safety set. A state removed in iteration k has failure probability>0 within k turns under every policy. Thus removed initial layouts cannot reach all collected+encounter almost surely. Surviving safety states are not by this computation alone proven live.'}
# Early loss certificate is independent of subsequent choices.
from fractions import Fraction
loss=[]
for s in sorted(set(roots[~alive[roots]])):
 risks=[]
 for a in range(ac[s]):
  t=ai[s,a]
  risk=Fraction(1) if t==-2 else sum((Fraction(float(bw[t,k])).limit_denominator(48) for k in range(bc[t]) if be[t,k]>0 or bi[t,k]==-2),Fraction(0))
  risks.append(risk)
 loss.append({'state_index':int(s),'minimum_first_turn_failure_probability_exact':str(min(risks)),'action_risk_distribution':{str(x):risks.count(x) for x in set(risks)}})
result['initial_failure_certificates']=loss
upper=Fraction(1)-sum((Fraction(loss[0]['minimum_first_turn_failure_probability_exact']) for _ in unsafe),Fraction(0))/len(roots)
result['uniform_initial_unbounded_all_goal_probability_upper_bound_exact']=str(upper)
result['uniform_initial_unbounded_all_goal_probability_upper_bound']=float(upper)
# Nested almost-sure reachability: a safe action must keep all successors in
# W and have at least one positive-probability successor already in R/goal.
W=alive.copy();outer_steps=[]
for outer in range(1,100):
 b_safe=np.where(bw>0,np.where(bi>=0,W[np.maximum(bi,0)]&(be==0),(bi==-2)&(free[:,None]==0)),True).all(axis=1)
 R=np.zeros(len(states),bool)
 for inner in range(1,10000):
  b_progress=b_safe&np.where(bw>0,np.where(bi>=0,R[np.maximum(bi,0)],(bi==-2)&(free[:,None]==0)),False).any(axis=1)
  next_R=W&np.where(ai>=0,b_progress[np.maximum(ai,0)],(ai==-2)&(free[:,None]==0)).any(axis=1)
  next_R|=R
  if np.array_equal(next_R,R):break
  R=next_R
 outer_steps.append({'outer':outer,'inner_iterations':inner,'winning_count':int(R.sum())})
 print('nested',outer_steps[-1],flush=True)
 if np.array_equal(W,R):break
 W=R
result['almost_sure_winning_state_count']=int(W.sum())
result['almost_sure_initial_winning_layout_count']=int(W[roots].sum())
result['almost_sure_nested_fixed_point_iterations']=outer_steps
result['almost_sure_equals_safety_set']=bool(np.array_equal(W,alive))
# If an unsafe initial layout can transition straight into W with probability
# 3/4, its upper bound is attained by switching to an almost-sure policy there.
attain=[]
for s in sorted(set(roots[~W[roots]])):
 chances=[]
 for a in range(ac[s]):
  t=ai[s,a]
  chance=Fraction(0) if t==-2 else sum((Fraction(float(bw[t,k])).limit_denominator(48) for k in range(bc[t]) if be[t,k]==0 and bi[t,k]>=0 and W[bi[t,k]]),Fraction(0))
  chances.append(chance)
 attain.append({'state_index':int(s),'best_first_turn_probability_entering_almost_sure_winning_set_exact':str(max(chances))})
result['initial_unsafe_attained_success_certificates']=attain
result['uniform_initial_unbounded_all_goal_optimum_exact']=str(upper) if all(Fraction(x['best_first_turn_probability_entering_almost_sure_winning_set_exact'])==1-Fraction(loss[0]['minimum_first_turn_failure_probability_exact']) for x in attain) and np.array_equal(W,alive) else None
(ROOT/'all_collect_safety.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
np.savez(ROOT/'all_collect_safety.npz',safe=alive,elimination=elimination,roots=roots,pairs=pairs)
print(json.dumps({k:v for k,v in result.items() if k not in ('iterations','unsafe_examples')},indent=2),flush=True)
print('elapsed',time.monotonic()-start,flush=True)
