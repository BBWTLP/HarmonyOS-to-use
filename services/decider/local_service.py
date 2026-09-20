"""Loopback-only bounded adapter for a pinned Decider snapshot (choice + noul)."""
import asyncio, contextlib, hmac, json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Literal
from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse

ROOT = Path(__file__).resolve().parent
REVISION = "7789eb65d5cf519737608e218fa88819bddea0af"
MODEL_PATH = ROOT / "models" / REVISION
MAX_BODY = 65536
TIMEOUT = 15.0

class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["choice"]
    instructions: str = Field(min_length=1, max_length=2000)
    criteria: dict[str, str] = Field(min_length=2, max_length=16)
    @model_validator(mode="after")
    def bounded(self):
        if any(not k or len(k)>128 or len(v)>1000 for k,v in self.criteria.items()):
            raise ValueError("invalid candidate key or description size")
        return self

class Noul(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["noul"]
    instructions: str = Field(min_length=1, max_length=2000)
    criteria: dict[str, str] | None = None
    @model_validator(mode="after")
    def bounded(self):
        if self.criteria is not None and (set(self.criteria)-{"true","false"} or any(len(v)>1000 for v in self.criteria.values())):
            raise ValueError("noul supports only bounded true/false criteria")
        return self

class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str | dict | list
    questions: dict[str, Annotated[Choice | Noul, Field(discriminator="type")]] = Field(min_length=1,max_length=4)
    independent: Literal[True] = True
    layout: Literal["state_first"] = "state_first"

class BodyLimit:
    def __init__(self,app): self.app=app
    async def __call__(self,scope,receive,send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope,receive,send)
        headers=dict(scope["headers"])
        if not headers.get(b"content-type",b"").lower().startswith(b"application/json"):
            return await JSONResponse({"detail":"application/json required"},415)(scope,receive,send)
        data=bytearray()
        while True:
            event=await receive()
            if event["type"]=="http.disconnect": return
            data.extend(event.get("body",b""))
            if len(data)>MAX_BODY:
                return await JSONResponse({"detail":"request body exceeds 64 KiB"},413)(scope,receive,send)
            if not event.get("more_body"): break
        delivered=False
        async def replay():
            nonlocal delivered
            if not delivered:
                delivered=True
                return {"type":"http.request","body":bytes(data),"more_body":False}
            return await receive()
        await self.app(scope,replay,send)

class SnapshotRunner:
    def __init__(self):
        import torch
        if not torch.cuda.is_available(): raise RuntimeError("CUDA unavailable; CPU fallback is not enabled")
        sys.path.insert(0,str(MODEL_PATH))
        from decider.infer import Decider
        self.model=Decider(str(MODEL_PATH),device="cuda",dtype=torch.bfloat16,use_graphs=False)
        self.model.m.lm.config.use_cache=False
        self.tokenizer=self.model.m.tok
        self.torch=torch
        # Readiness requires a real forward pass.
        self({"state":"The page title is Home.","questions":{"ready":{"type":"noul","instructions":"Is the page title Home?"}},"independent":True,"layout":"state_first"})

    def __call__(self,payload):
        from decider.systemone import render_state,render_question
        from decider.prompt import build, MAX_OPTIONS
        from decider.infer import Example,Q
        state=render_state(payload["state"])
        if len(self.tokenizer.encode(state,add_special_tokens=False))>1024:
            raise ValueError("state exceeds 1024 tokens; refresh or compact evidence")
        class Keep:
            def shuffle(self,x): pass
            def sample(self,x,k): return x[:k]
        for spec in payload["questions"].values():
            r=render_question(spec)
            item=build(Example(state,[Q(r["question"],r["options"])]),self.tokenizer,Keep(),max_options=MAX_OPTIONS,max_ctx_tokens=1024,layout="state_first")
            if len(item["ids"])>1536: raise ValueError("rendered question exceeds 1536 tokens")
        start=time.perf_counter()
        output=self.model.system_one(**payload,max_state_tokens=1024,max_fwd_tokens=1536)
        self.torch.cuda.synchronize()
        output["deployment"]={"model_id":"Mapika/decider-2b","model_revision":REVISION,"precision":"bfloat16","backend":"pytorch_cuda_eager","layout":"state_first","project_calibration":None,"mode":"shadow_only","inference_ms":round((time.perf_counter()-start)*1000,2)}
        return output

def create_app(runner_factory=SnapshotRunner, token=None, timeout=TIMEOUT):
    pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix="decider")
    @contextlib.asynccontextmanager
    async def lifespan(app):
        app.state.token=token or (ROOT/".runtime/api-token").read_text().strip()
        if not app.state.token: raise RuntimeError("Empty API token")
        app.state.busy=False
        app.state.ready=False
        try:
            app.state.runner=await asyncio.get_running_loop().run_in_executor(pool,runner_factory)
            app.state.ready=True
            yield
        finally:
            app.state.ready=False
            pool.shutdown(wait=True)
    app=FastAPI(title="Local Decider 2B",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=["127.0.0.1","localhost","testserver"])
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        return JSONResponse({"detail":"invalid decision request","errors":[{"loc":list(e['loc']),"type":e['type']} for e in exc.errors()]},422)
    def authorized(authorization):
        if not authorization or not hmac.compare_digest(authorization,"Bearer "+app.state.token):
            raise HTTPException(401,"local service token required")
    @app.get("/health")
    async def health():
        return {"ok":getattr(app.state,"ready",False),"model":"Mapika/decider-2b","revision":REVISION,"mode":"shadow_only","busy":getattr(app.state,"busy",False)}
    @app.get("/v1/models")
    async def models(authorization: str | None=Header(default=None)):
        authorized(authorization)
        return {"data":[{"id":"Mapika/decider-2b","revision":REVISION,"capabilities":["choice","noul"],"layout":"state_first","max_candidates":16,"project_calibration":None}]}
    @app.post("/v1/systemone")
    async def decide(r:Request,authorization: str | None=Header(default=None)):
        authorized(authorization)
        if app.state.busy: raise HTTPException(503,"model busy; use baseline controller")
        app.state.busy=True
        def work():
            return app.state.runner(r.model_dump(exclude_none=True))
        future=asyncio.get_running_loop().run_in_executor(pool,work)
        def completed(f):
            app.state.busy=False
            if not f.cancelled(): f.exception() # consume late failure without logging request content
        future.add_done_callback(completed)
        try:
            return await asyncio.wait_for(asyncio.shield(future),timeout)
        except asyncio.TimeoutError:
            raise HTTPException(504,"inference deadline exceeded; GPU work drains before another request")
        except ValueError as e:
            raise HTTPException(422,str(e))
        except Exception:
            raise HTTPException(503,"model inference failed; use baseline controller")
    return app

app=create_app()
