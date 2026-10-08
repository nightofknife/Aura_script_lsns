"""Atomic command producer for the owned vertical research capture broker."""
from pathlib import Path
import json,sys,time


def issue(folder,number,command,timeout=30.):
    folder=Path(folder);commands=folder/'commands'
    path=commands/f'{number:04d}.json';temporary=commands/f'{number:04d}.tmp'
    if path.exists():raise RuntimeError('command_already_published')
    temporary.write_text(json.dumps(command,ensure_ascii=False),'utf8');temporary.replace(path)
    result_path=commands/f'{number:04d}_result.json';deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if result_path.exists():
            try:result=json.loads(result_path.read_text('utf8'))
            except (json.JSONDecodeError,PermissionError):time.sleep(.02);continue
            if result['status'] not in ('success','closed'):raise RuntimeError(json.dumps(result))
            return result
        if (folder/'cleanup.json').exists():raise RuntimeError('broker_closed_before_response')
        time.sleep(.05)
    raise TimeoutError('broker_response_not_received')


if __name__=='__main__':
    result=issue(sys.argv[1],int(sys.argv[2]),json.loads(sys.argv[3]))
    print(json.dumps(result,ensure_ascii=False))
