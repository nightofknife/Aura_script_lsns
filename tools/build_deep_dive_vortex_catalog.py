"""Build the isolated vortex allowlist/resources from reviewed local exports."""
import argparse
import json
import re
import shutil
from pathlib import Path


NATIVE_BATTLE_LEVELS = {
    (89800039, 86100250): 20101954,
    (89800042, 86100259): 20101959,  # 狭路相逢 · 手下败将不足为惧
    (89800040, 86100252): 20101955,  # 防御系统故障 · 原地迎战
    (89800085, 86100297): 20101977,  # 原型机 · 测一下！
    (89800078, 86100178): 20101997,  # 扭蛋机后段 · 什么？！
}


def _dust_reward(minimum, maximum):
    return ('Reward', ((11400607, minimum, maximum),), ((11400607, None, None, None, None),), 1)


_NATIVE_OPTION_EFFECTS = {
    # 防御系统故障 · 找个地方躲起来
    (89800040, 86100253): (('Recover', 'Present', -0.2), _dust_reward(50, 150)),
    # 地板塌陷 · 快速通过
    (89800027, 86100210): (('Recover', 'Present', -0.1), _dust_reward(100, 100)),
    # 求真者 · 观察她
    (89800011, 86100120): (
        ('Reward', ((86100287, 1, 1), (86100282, 1, 1)),
         ((86100287, 'Equip', 'Random', 1, 1), (86100282, 'Buff', 'Random', 1, 1)), 1),
    ),
    # 原型机 · 为啥要帮你啊？！
    (89800085, 86100298): (
        ('Reward', ((86100167, 1, 1),), ((86100167, 'Equip', 'Random', 1, 1),), 1),
        ('Event', 89800086),
    ),
    # 骑士的承诺 · 有什么有用的东西吗？
    (89800076, 86100339): (_dust_reward(100, 100),),
    # 狭路相逢 · 以和为贵
    (89800042, 86100258): (
        ('Discard', ((11400607, 100),), 1, True),
        ('Reward', ((86100162, 1, 1),), ((86100162, 'Buff', 'Random', 2, 2),), 1),
    ),
}


def _effect_signature(effect):
    kind = effect['orderType']
    if kind == 'Recover':
        return kind, effect.get('recoverType'), effect.get('param')
    if kind == 'Reward':
        rewards = tuple((row['id'], row.get('numMin'), row.get('numMax'))
                        for row in effect.get('rewardList', []))
        packages = tuple((row['id'], row.get('packageType'), row.get('func'),
                          row.get('refreshNum'), row.get('selectNum')) for row in effect.get('packages', []))
        return kind, rewards, packages, effect.get('probability', 1)
    if kind == 'Discard':
        costs = tuple((row['id'], row['num']) for row in effect.get('discardList', []))
        return kind, costs, effect.get('param'), effect.get('isEnough')
    if kind == 'Event':
        return kind, effect.get('eventId')
    return kind,


def option_policy(effects, single=False, recurring=False):
    if not effects:
        return dict(group='End',blocked=False,supported=True,
                    unsupported_reasons=[],priority=-10)
    blocked=False; reasons=[]; groups=[]
    if single:reasons.append('单选项事件布局')
    if recurring:reasons.append('跨次遭遇事件')
    for e in effects:
        typ=e['orderType']
        if typ in ('Action','StepNum') or typ=='SpinNum' and e.get('param',0)>0:
            blocked=True
        if typ=='Recover':groups.append('Recover+' if e['param']>0 else 'Recover-')
        elif typ=='Reward':
            packages=e.get('packages',[])
            refs=e.get('rewardList',[])
            if (len(refs)==1 and refs[0].get('id')==11400607
                    and 0 < refs[0].get('numMin',0) <= refs[0].get('numMax',0)):
                groups.append('Reward.Currency')
                if len(effects)!=1:reasons.append('尘埃组合效果')
                continue
            if len(packages)!=1:reasons.append('多件或混合奖励')
            if any(r.get('numMax',1)!=1 or r.get('numMin',1)!=1 for r in refs):
                reasons.append('奖励数量或货币')
            for p in packages:
                if p.get('packageType') not in ('Buff','Equip'):
                    reasons.append('尘埃或未知奖励')
                elif not (p.get('selectNum')==1 and
                          (p.get('func')=='Random' and p.get('refreshNum')==1 or
                           p.get('func')=='Select' and p.get('refreshNum')==3)):
                    reasons.append('多件或特殊奖励包')
                groups.append('Reward.'+str(p.get('packageType'))+'.'+str(p.get('func')))
        else:
            groups.append(typ)
            if typ in ('LoseBuff','LoseEquip'):
                if e.get('loseType')!='Select' or e.get('param')!=1:reasons.append('随机或多件丢弃')
            elif typ=='Intensify':
                if e.get('param')!=1:reasons.append('多目标强化')
            elif typ not in ('Action','StepNum','SpinNum'):
                reasons.append({'Level':'事件内战斗','Event':'下一组选项','Discard':'尘埃扣除'}.get(typ,'未知效果'))
    group=':'.join(groups) or 'End'
    return dict(group=group,blocked=blocked,supported=not reasons,
                unsupported_reasons=sorted(set(reasons)),
                priority=0 if group=='Recover+' else 20 if group=='End' else 10)


def reviewed_option_policy(event_id, option_id, effects, single=False, recurring=False):
    policy = option_policy(effects, single, recurring)
    if policy['blocked']:
        return policy

    key = event_id, option_id
    battle_level = NATIVE_BATTLE_LEVELS.get(key)
    if (battle_level is not None and len(effects) == 1
            and effects[0]['orderType'] == 'Level' and effects[0].get('levelId') == battle_level):
        policy['battle_level'] = battle_level

    expected = _NATIVE_OPTION_EFFECTS.get(key)
    if expected is not None and tuple(map(_effect_signature, effects)) == expected:
        policy.update(supported=True, native_only=True, unsupported_reasons=[])
        for kind, *values in expected:
            if kind == 'Event':
                policy['next_event_id'] = values[0]
    return policy


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--expanded',type=Path,required=True)
    ap.add_argument('--review',type=Path,required=True)
    ap.add_argument('--plan',type=Path,default=Path('plans/resonance_pc'))
    args=ap.parse_args()
    source=json.loads(args.expanded.read_text(encoding='utf8'))
    review=json.loads((args.review/'catalog.json').read_text(encoding='utf8'))
    refs={o['id']:o for o in review['options']}
    events=[];options={}
    for event in source:
        es=event['options_expanded']
        events.append({'id':event['id'],'name':event['name'],'variant':event['idCN'],
                       'options':[o['id'] for o in es]})
        for o in es:
            effects = o['effects']
            policy = reviewed_option_policy(
                event['id'], o['id'], effects, single=len(es) == 1,
                recurring='/多次触发/' in event['idCN'],
            )
            options[o['id']]={'id':o['id'],'title':o['optionName'],
                'description':re.sub('<[^>]+>','',o['optionDes']),
                'effects':effects,'text_key':refs[o['id']]['text_key'],**policy}
    output=args.plan/'templates/deep_dive/deep_dive_vortex';output.mkdir(parents=True,exist_ok=True)
    for key in {o['text_key'] for o in options.values()}:
        for state in ('normal','disabled','selected'):
            name=key+'_'+state+'.png';shutil.copyfile(args.review/name,output/name)
    # Physical copies: changing healing templates later cannot change vortex.
    for path in (args.plan/'templates/deep_dive/deep_dive_events').glob('*.png'):
        if not path.name.startswith('text_'):shutil.copyfile(path,output/path.name)
    out=args.plan/'data/deep_dive_vortex';out.mkdir(parents=True,exist_ok=True)
    data={'source_hashes':review['source_hashes'],'events':events,'options':list(options.values())}
    (out/'catalog.json').write_text(json.dumps(data,ensure_ascii=False,indent=2) + '\n',encoding='utf8')
    print(json.dumps({'events':len(events),'options':len(options),'supported_allowed':sum(o['supported'] and not o['blocked'] for o in options.values()),
                      'blocked':sum(o['blocked'] for o in options.values()),
                      'unsupported_allowed':sum(not o['supported'] and not o['blocked'] for o in options.values())}))


if __name__=='__main__':main()
