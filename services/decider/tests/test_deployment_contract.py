"""Boundary tests run with a fake model; no CUDA or phone needed."""
import asyncio, copy, sys, threading, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from local_service import create_app
PAYLOAD={"state":"Page title: Home","questions":{"action":{"type":"choice","instructions":"Choose the visible title","criteria":{"a":"Home","b":"Settings"}}}}
HEADERS={"Authorization":"Bearer test-secret"}
class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls=[]
        def runner(p): self.calls.append(p); return {"ok":True}
        self.app=create_app(lambda:runner,token="test-secret")
        self.life=self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url="http://127.0.0.1")
    async def asyncTearDown(self):
        await self.client.aclose(); await self.life.__aexit__(None,None,None)
    async def test_auth_and_readiness(self):
        self.assertTrue((await self.client.get('/health')).json()['ok'])
        self.assertEqual((await self.client.post('/v1/systemone',json=PAYLOAD)).status_code,401)
        self.assertEqual((await self.client.get('/v1/models')).status_code,401)
        self.assertEqual((await self.client.post('/v1/systemone',json=PAYLOAD,headers=HEADERS)).status_code,200)
        self.assertEqual(len(self.calls),1)
    async def test_schema_rejects_unbounded_and_unsupported(self):
        bads=[]
        for field,value in [('independent',False),('layout','schema_first'),('unknown',1)]:
            p=copy.deepcopy(PAYLOAD); p[field]=value; bads.append(p)
        p=copy.deepcopy(PAYLOAD); p['questions']['action']['criteria']={str(i):'x' for i in range(17)}; bads.append(p)
        p=copy.deepcopy(PAYLOAD); p['questions']['action']['type']='score'; bads.append(p)
        p=copy.deepcopy(PAYLOAD); p['questions']={str(i):p['questions']['action'] for i in range(5)}; bads.append(p)
        p={'state':'x','questions':{'x':{'type':'noul','instructions':'x','criteria':{'maybe':'x'}}}}; bads.append(p)
        for p in bads:
            self.assertEqual((await self.client.post('/v1/systemone',json=p,headers=HEADERS)).status_code,422,p)
        self.assertEqual(len(self.calls),0)
    async def test_body_content_type_and_host(self):
        self.assertEqual((await self.client.post('/v1/systemone',content=b'{}',headers=HEADERS)).status_code,415)
        self.assertEqual((await self.client.post('/v1/systemone',content=b'x'*65537,headers={**HEADERS,'Content-Type':'application/json'})).status_code,413)
        self.assertEqual((await self.client.get('/health',headers={'Host':'evil.invalid'})).status_code,400)
        self.assertEqual((await self.client.get('/docs')).status_code,404)
        self.assertEqual(len(self.calls),0)
    async def test_timeout_does_not_release_busy_before_drain(self):
        entered=threading.Event(); release=threading.Event(); count=[]
        def slow(p):
            count.append(1); entered.set(); release.wait(2); return {'ok':True}
        app=create_app(lambda:slow,token='test-secret',timeout=0.04)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
                try:
                    task=asyncio.create_task(client.post('/v1/systemone',json=PAYLOAD,headers=HEADERS))
                    for _ in range(100):
                        if entered.is_set(): break
                        await asyncio.sleep(0.005)
                    self.assertTrue(entered.is_set())
                    self.assertEqual((await client.post('/v1/systemone',json=PAYLOAD,headers=HEADERS)).status_code,503)
                    self.assertEqual((await task).status_code,504)
                    self.assertTrue((await client.get('/health')).json()['busy'])
                    self.assertEqual((await client.post('/v1/systemone',json=PAYLOAD,headers=HEADERS)).status_code,503)
                    self.assertEqual(len(count),1)
                finally: release.set()
                for _ in range(100):
                    if not app.state.busy: break
                    await asyncio.sleep(0.005)
                self.assertFalse(app.state.busy)
                self.assertEqual((await client.post('/v1/systemone',json=PAYLOAD,headers=HEADERS)).status_code,200)
    async def test_model_errors_fail_closed(self):
        def fail(p): raise RuntimeError('private model internals')
        self.app.state.runner=fail
        response=await self.client.post('/v1/systemone',json=PAYLOAD,headers=HEADERS)
        self.assertEqual(response.status_code,503)
        self.assertNotIn('private',response.text)
if __name__=='__main__': unittest.main(verbosity=2)
