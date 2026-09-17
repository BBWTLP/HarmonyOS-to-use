"""Explicit, opt-in real-device read-only MCP smoke; keeps raw UI out of stdout."""
import asyncio
import json
import sys
import tempfile
import threading
from harmony_runtime.service import serve

from harmony_runtime.probe import probe

async def check(root):
    result = await probe(root)
    print(json.dumps(result))
    return result["status"] == "ok"

def main():
    with tempfile.TemporaryDirectory() as root:
        ready=threading.Event();servers=[]
        def notify(server):servers.append(server);ready.set()
        thread=threading.Thread(target=serve,args=(root,),kwargs={"ready":notify})
        thread.start()
        if not ready.wait(10): raise RuntimeError("Runtime did not start")
        try: success = asyncio.run(check(root))
        finally: servers[0].shutdown();thread.join()
        return 0 if success else 1

if __name__=="__main__": sys.exit(main())
