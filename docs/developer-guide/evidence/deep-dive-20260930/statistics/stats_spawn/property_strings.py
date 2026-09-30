import sys,json
from pathlib import Path
base=Path('.analysis_tmp/deep_dive_planning_20260930/cube/disasm.py').read_text(encoding='utf-8').split('\nops = ')[0]
def decode_string(b):
    try:return b.decode('utf-8')
    except UnicodeDecodeError:return b.decode('gb18030',errors='replace')
base=base.replace("self.take(n-1).decode('utf-8', 'replace')",'decode_string(self.take(n-1))')
exec(base)
node=Reader('D:/software/soli/Resonance/雷索纳斯_Data/Patch/Script/FactoryRegister/Propertys/CubeRogueFactory.lua').proto()
constants=node['constants']
for i,c in enumerate(constants):
    if c in ('specialNum','certainlyGenerateList','specialGenerateList','numMax','numMin','typeLimitList','cubeList','bossMuseNum','museSkill'):
        print(json.dumps(constants[max(0,i-2):i+8],ensure_ascii=False))
