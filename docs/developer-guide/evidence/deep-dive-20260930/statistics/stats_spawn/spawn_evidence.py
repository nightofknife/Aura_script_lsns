"""Read local client bytes and config; never execute game Lua."""
import json, struct, sys
from pathlib import Path
sys.path.insert(0, 'D:/project/Aura_script_lsns_devtools/src')
from aura_resonance_devtools.binary_config import PooledBinaryConfig
from aura_resonance_devtools.role_catalog import IndexedConfigReader
root = Path('D:/software/soli/Resonance/雷索纳斯_Data/Patch')
cfg = PooledBinaryConfig.load(root/'BinaryConfig/CubeRogueFactory.bin')
rd = IndexedConfigReader(cfg, 'CubeRogueFactory.bin')
r = next(r for r in rd.records if r.record_id == 89400001)
positions=[]
for key in cfg.strings.values():
    if key.isidentifier():
        off=cfg.string_offset(key)
        for t in range(1,16):
            p=cfg.data.find(struct.pack('<II',t,off),r.start,r.end)
            if p>=0: positions.append((p,key,t))
positions.sort()
for i,(p,key,t) in enumerate(positions):
    if key in ('specialNum','specialGenerateList','certainlyGenerateList','typeLimitList','cubeList','initialX','initialY','initialZ','x','y','z'):
        end=positions[i+1][0] if i+1<len(positions) else r.end
        raw=cfg.data[p:end]
        print('CONFIG',key,'type',t,'bytes',raw.hex(),'u32',[struct.unpack_from('<I',raw,j)[0] for j in range(9,len(raw)-3,4)])
base=Path('.analysis_tmp/deep_dive_planning_20260930/cube/disasm.py').read_text(encoding='utf-8').split('\nops = ')[0]
namespace={}
exec(base,namespace)
Reader=namespace['Reader']
def visit(n, source):
    const=n['constants']
    if any(c in const for c in ('specialGenerateList','certainlyGenerateList','typeLimitList','SetData','faces','CubeRogueFactory')):
        print('LUA',source.name,n['line'],n['end'],json.dumps(const,ensure_ascii=False))
    for child in n['children']:visit(child,source)
for d in ('UICubeRogueMain','UIActivityCubeRogue'):
    for source in (root/'Script'/d).glob('*.lua'):
        visit(Reader(source).proto(),source)
