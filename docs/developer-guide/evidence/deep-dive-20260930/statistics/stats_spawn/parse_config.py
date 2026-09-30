import json,struct,sys
from pathlib import Path
sys.path.insert(0,'D:/project/Aura_script_lsns_devtools/src')
from aura_resonance_devtools.binary_config import PooledBinaryConfig
from aura_resonance_devtools.role_catalog import IndexedConfigReader
c=PooledBinaryConfig.load('D:/software/soli/Resonance/雷索纳斯_Data/Patch/BinaryConfig/CubeRogueFactory.bin')
data=c.data
u=lambda p:struct.unpack_from('<I',data,p)[0]
def field(p):
    size,key,kind=u(p),c.strings[u(p+4)],data[p+8]
    val=p+9
    if kind==7:
        length=u(val); tag=data[val+4]; count=u(val+5); cursor=val+9; array=[]
        for _ in range(count):
            length=u(cursor); elem,stop=record(cursor+4,cursor+4+length);array.append(elem);cursor=stop
        result=array
    elif kind==1:result=struct.unpack_from('<d',data,val)[0]
    elif kind==0:result=u(val)
    elif kind==6:result={'reference':u(val)}
    elif kind in (4,5,9,12):result=c.strings.get(u(val),{'pool_reference':u(val)})
    else:result={'kind':kind,'hex':data[val:p+4+size].hex()}
    return key,result,p+4+size
def record(p,end):
    count=u(p);p+=4;result={}
    for _ in range(count):
        key,v,p=field(p);result[key]=v
    assert p==end,(p,end)
    return result,p
r=next(r for r in IndexedConfigReader(c,'x').records if r.record_id==89400001)
result,stop=record(r.start-4,r.end)
print(json.dumps(result,ensure_ascii=False,indent=2))
