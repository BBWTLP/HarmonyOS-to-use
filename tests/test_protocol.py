import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from harmony_runtime.service import serve, Client, Singleton
from harmony_runtime.runtime import Runtime
from test_runtime import FakeDevice

class ProtocolTests(unittest.TestCase):
    def test_discovery_without_runtime(self):
        async def probe(root):
            args=StdioServerParameters(command=sys.executable,args=["-m","harmony_runtime.cli","mcp","--state-dir",root],env={"PYTHONPATH":str(Path(__file__).resolve().parents[1]/"src")})
            async with stdio_client(args) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    tools=await client.list_tools()
                    self.assertEqual({t.name for t in tools.tools},{"mobile_session","mobile_observe","mobile_act","mobile_burst","mobile_wait","mobile_history"})
                    missing=await client.call_tool("mobile_session",{"operation":"open"})
                    self.assertTrue(missing.is_error)
                    self.assertEqual(missing.structured_content["error"]["code"],"runtime_unavailable")
            self.assertFalse((Path(root)/"endpoint.json").exists())
        with tempfile.TemporaryDirectory() as root: asyncio.run(probe(root))
    def test_expected_fault_survives_stdio(self):
        async def probe(root):
            args=StdioServerParameters(command=sys.executable,args=["-m","harmony_runtime.cli","mcp","--state-dir",root],env={"PYTHONPATH":str(Path(__file__).resolve().parents[1]/"src")})
            async with stdio_client(args) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    opened=await client.call_tool("mobile_session",{"operation":"open"})
                    self.assertFalse(opened.is_error)
                    sid=opened.structured_content["session_id"]
                    queried=await client.call_tool("mobile_session",{"operation":"action_status","session_id":sid,"request_id":"no-dispatch"})
                    self.assertFalse(queried.is_error)
                    self.assertEqual(queried.structured_content["status"],"not_found")
                    self.assertIn("not proof",queried.structured_content["message"])
                    for tool,params,code in [
                        ("mobile_observe",{"session_id":sid},"screen_locked"),
                        ("mobile_wait",{"session_id":sid,"expected":{"text":"Settings"}},"screen_locked"),
                        ("mobile_session",{"operation":"status","session_id":"missing"},"session_invalid")
                    ]:
                        result=await client.call_tool(tool,params)
                        self.assertTrue(result.is_error)
                        self.assertEqual(result.structured_content["error"]["code"],code)
                        self.assertNotIn("Error executing tool",result.content[0].text)
                    await client.call_tool("mobile_session",{"operation":"close","session_id":sid})
        with tempfile.TemporaryDirectory() as root:
            event=threading.Event();servers=[]
            device=FakeDevice("fake")
            device.screen_state=lambda:{"screen_on":True,"screen_locked":True}
            def ready(server):servers.append(server);event.set()
            thread=threading.Thread(target=serve,args=(root,),kwargs={"runtime_factory":lambda p:Runtime(p,factory=lambda serial:device,discover=lambda:["fake"]),"ready":ready})
            thread.start()
            self.assertTrue(event.wait(10))
            try:
                asyncio.run(probe(root))
                self.assertEqual(device.writes,0)
            finally:
                servers[0].shutdown();thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_full_observation_images_through_stdio(self):
        from test_full_observation import ImageDevice
        class DeepImageDevice(ImageDevice):
            def tree(self):
                node = super().tree()
                for _ in range(140):
                    node = {"attributes": {"type": "Column"}, "children": [node]}
                return node
        async def probe(root):
            args=StdioServerParameters(command=sys.executable,args=["-m","harmony_runtime.cli","mcp","--state-dir",root],env={"PYTHONPATH":str(Path(__file__).resolve().parents[1]/"src")})
            async with stdio_client(args) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    opened = await client.call_tool("mobile_session", {"operation":"open"})
                    sid = opened.structured_content["session_id"]
                    result = await client.call_tool("mobile_observe", {"session_id":sid, "mode":"FULL"})
                    self.assertFalse(result.is_error)
                    data = result.structured_content
                    self.assertEqual(data["mode"], "FULL")
                    self.assertTrue(data["som"]["available"])
                    self.assertEqual(data["tree"]["format"], "flat_tree_v1")
                    self.assertEqual(len(data["tree"]["nodes"]), 141)
                    self.assertNotIn("image", data)
                    self.assertNotIn("annotated_image", data)
                    images = [item for item in result.content if item.type == "image"]
                    self.assertEqual(len(images), 2)
                    self.assertTrue(all(item.mime_type == "image/png" for item in images))
                    self.assertNotEqual(images[0].data, images[1].data)
                    action = await client.call_tool("mobile_act", {"request": {
                        "session_id":sid, "request_id":"full-protocol", "observation_id":data["observation_id"],
                        "action":{"kind":"tap", "target":{"action_id":data["som"]["labels"][0]["action_id"]}}}})
                    self.assertFalse(action.is_error)
                    self.assertEqual(action.structured_content["execution_status"], "executed")
                    temporal = await client.call_tool("mobile_observe", {"session_id":sid, "mode":"TEMPORAL"})
                    self.assertFalse(temporal.is_error)
                    self.assertEqual(len([item for item in temporal.content if item.type == "image"]), 5)
                    self.assertEqual(len(temporal.structured_content["frames"]), 5)
                    self.assertTrue(all("image" not in frame for frame in temporal.structured_content["frames"]))
                    self.assertTrue(all(not frame["actionable"] for frame in temporal.structured_content["frames"]))
                    await client.call_tool("mobile_session", {"operation":"close", "session_id":sid})
        with tempfile.TemporaryDirectory() as root:
            event=threading.Event(); servers=[]
            device=DeepImageDevice("fake")
            def ready(server): servers.append(server); event.set()
            thread=threading.Thread(target=serve,args=(root,),kwargs={"runtime_factory":lambda p:Runtime(p,factory=lambda _:device,discover=lambda:["fake"]),"ready":ready})
            thread.start()
            self.assertTrue(event.wait(10))
            try:
                asyncio.run(probe(root))
                self.assertEqual(device.captures, 6)
                self.assertEqual(device.writes, 1)
            finally:
                servers[0].shutdown();thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_burst_through_stdio_and_shared_service(self):
        from test_burst import Pages
        async def probe(root):
            args = StdioServerParameters(command=sys.executable,
                args=["-m", "harmony_runtime.cli", "mcp", "--state-dir", root],
                env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
            async with stdio_client(args) as streams:
                async with ClientSession(*streams) as client:
                    await client.initialize()
                    opened = await client.call_tool("mobile_session", {"operation": "open"})
                    sid = opened.structured_content["session_id"]
                    observed = await client.call_tool("mobile_observe", {"session_id": sid})
                    request = dict(session_id=sid, request_id="protocol-sequence",
                        observation_id=observed.structured_content["observation_id"],
                        steps=[dict(action=dict(kind="tap", target=dict(text=f"Page{i}")),
                                    expected=dict(text=f"Page{i+1}"), watch_timeout_ms=1000) for i in range(3)])
                    for repeat in range(2):
                        result = await client.call_tool("mobile_burst", {"request": request})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content["status"], "completed")
                        self.assertEqual(result.structured_content["verified_steps"], 3)
                        if repeat:
                            self.assertTrue(result.structured_content["deduplicated"])
                    for condition in [
                        dict(type="text_absent", value="Page0"),
                        dict(type="element_present", target=dict(text="Page3")),
                        dict(type="change", observation_id=observed.structured_content["observation_id"]),
                        dict(type="stable", stable_ms=100),
                    ]:
                        # The original observation is invalidated by writes; a
                        # change wait must reject that old handle over MCP too.
                        waited = await client.call_tool("mobile_wait", dict(session_id=sid,
                            condition=condition, timeout_ms=1000, poll_ms=100))
                        if condition["type"] == "change":
                            self.assertTrue(waited.is_error)
                            self.assertEqual(waited.structured_content["error"]["code"], "stale_observation")
                        else:
                            self.assertFalse(waited.is_error)
                            self.assertEqual(waited.structured_content["status"], "matched")
                    history = await client.call_tool("mobile_history", dict(session_id=sid, limit=2))
                    self.assertFalse(history.is_error)
                    page = history.structured_content
                    self.assertEqual(len(page["items"]), 2)
                    tail = await client.call_tool("mobile_history", dict(session_id=sid, limit=2,
                        before=page["next_before"]))
                    self.assertFalse(tail.is_error)
                    self.assertEqual(len(tail.structured_content["items"]), 1)
                    self.assertIsNone(tail.structured_content["next_before"])
                    status = await client.call_tool("mobile_session", dict(operation="burst_status",
                        session_id=sid, request_id="protocol-sequence"))
                    self.assertFalse(status.is_error)
                    self.assertEqual(len(status.structured_content["children"]), 3)
                    await client.call_tool("mobile_session", dict(operation="close", session_id=sid))
        with tempfile.TemporaryDirectory() as root:
            event = threading.Event(); servers = []; device = Pages("fake")
            def ready(server): servers.append(server); event.set()
            thread = threading.Thread(target=serve, args=(root,), kwargs={
                "runtime_factory": lambda p: Runtime(p, factory=lambda _: device, discover=lambda: ["fake"]),
                "ready": ready})
            thread.start()
            self.assertTrue(event.wait(10))
            try:
                asyncio.run(probe(root))
                self.assertEqual(device.targets, ["Page0", "Page1", "Page2"])
                self.assertEqual(device.writes, 3)
            finally:
                servers[0].shutdown(); thread.join(10)
            self.assertFalse(thread.is_alive())

    def test_shared_service_and_lease(self):
        with tempfile.TemporaryDirectory() as root:
            event=threading.Event();servers=[];devices=[]
            def factory(serial):
                d=FakeDevice(serial);devices.append(d);return d
            def ready(server):servers.append(server);event.set()
            thread=threading.Thread(target=serve,args=(root,),kwargs={"runtime_factory":lambda p:Runtime(p,factory=factory,discover=lambda:["fake"]),"ready":ready})
            thread.start()
            self.assertTrue(event.wait(10))
            try:
                one,two=Client(root),Client(root)
                sid=one.call("session",operation="open")["session_id"]
                self.assertEqual(devices,[])
                with self.assertRaises(Exception) as ctx:two.call("session",operation="open")
                self.assertEqual(ctx.exception.code,"lease_conflict")
                one.call("observe",session_id=sid)
                one.call("observe",session_id=sid)
                self.assertEqual(len(devices),1)
                with self.assertRaises(OSError):
                    with Singleton(root):pass
                one.call("session",operation="close",session_id=sid)
                sid2=two.call("session",operation="open")["session_id"]
                two.call("observe",session_id=sid2)
                self.assertEqual(len(devices),1)
            finally:
                servers[0].shutdown();thread.join(10)
            self.assertFalse(thread.is_alive())

if __name__ == "__main__":unittest.main()
