exec(open('.analysis_tmp/deep_dive_planning_20260930/stats_spawn/parse_config.py',encoding='utf-8').read().split('r=next(')[0])
c=PooledBinaryConfig.load('D:/software/soli/Resonance/雷索纳斯_Data/Patch/BinaryConfig/ActivityListFactory.bin')
data=c.data
reader=IndexedConfigReader(c,'ActivityListFactory.bin')
for i in (86100103,86100362):
    r=next(r for r in reader.records if r.record_id==i)
    print(json.dumps(record(r.start-4,r.end)[0],ensure_ascii=False,indent=2))
