"""DR caller tests with scripted inference and real MCP protocol serialization.

Run: python -m unittest discover -s tests -p 'test_parallel_search.py'
The HTTP transport is deterministic; no inference or search credentials are used.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from mcp.client.streamable_http import streamablehttp_client
import yaml

DR_ROOT = Path(__file__).resolve().parents[1] / "internagent/mas/agents/dr_agents"
sys.path.insert(0, str(DR_ROOT))

import tools
from tools import ToolManager
from tools import parallel_search
from agents.task.execution_agent import ExecutionAgent
from agents.global_execution_agent import GlobalExecutionAgent
from workflow.main import Workflow
from utils.graph import DirectedGraph
from utils.search_context import search_run


class MCPServer:
    """Respond at the HTTP boundary so the production SDK handles the protocol."""

    def __init__(self):
        self.requests = []
        self.error = None
        self.delay = False
        self.structured = True

    async def handle(self, request):
        self.requests.append(request)
        if request.method != "POST":
            return httpx.Response(405)
        message = json.loads(request.content)
        method = message["method"]
        if "id" not in message:
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "test-search", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [
                {"name": name, "inputSchema": {"type": "object"}}
                for name in ("web_search", "web_fetch")
            ]}
        elif method == "tools/call":
            if self.delay:
                await asyncio.Event().wait()
            arguments = message["params"]["arguments"]
            url = arguments.get("urls", ["https://example.org/research"])[0]
            data = {"results": [{"url": url, "title": "Research source",
                                 "excerpts": ["Evidence about the research question."]}]}
            result = {"content": [{"type": "text", "text": json.dumps(data)}]}
            if self.structured:
                result["structuredContent"] = data
            if self.error:
                result = {"isError": True,
                          "content": [{"type": "text", "text": self.error}]}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"],
                                         "result": result})

    def transport(self, *args, **kwargs):
        def factory(**options):
            return httpx.AsyncClient(transport=httpx.MockTransport(self.handle), **options)
        return streamablehttp_client(*args, **kwargs, httpx_client_factory=factory)

    def calls(self):
        return [json.loads(r.content)["params"] for r in self.requests
                if r.method == "POST" and
                json.loads(r.content).get("method") == "tools/call"]


class ScriptedModel:
    """Model responses are scripted; tool loading, dispatch and consumption are real."""

    def __init__(self, barrier=None):
        self.messages = []
        self.barrier = barrier

    def generate_with_tools(self, messages, tools):
        self.messages.append(list(messages))
        step = sum(message["role"] == "tool" for message in messages)
        if step == 0 and self.barrier:
            self.barrier.wait(timeout=10)
        calls = [("search_parallel", {"query": "recent research evidence"}),
                 ("fetch_parallel", {"url": "https://example.org/research"}),
                 ("search_parallel", {"query": "follow up research evidence"})]
        if step >= len(calls):
            return {"choices": [{"message": {"role": "assistant", "content": "Done [[1]]"}}]}
        name, arguments = calls[step]
        assert name in {tool["function"]["name"] for tool in tools}
        return {"choices": [{"message": {"role": "assistant", "tool_calls": [{
            "id": f"call-{step}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }]}}]}

    def generate(self, *args, **kwargs):
        return "Research answer [[1]]"


class ParallelCallerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Retain proxy/network settings, but isolate all provider credentials and
        # disable .env loading so saved credentials cannot affect anonymous tests.
        environment = {key: value for key, value in os.environ.items()
                       if not any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET"))}
        self.stack.enter_context(patch.dict(os.environ, environment, clear=True))
        self.stack.enter_context(patch("dotenv.load_dotenv"))
        self.stack.enter_context(patch.object(tools, "tool_manager", None))
        self.stack.enter_context(patch("agents.task.execution_agent.get_model",
                                       side_effect=lambda *a, **kw: ScriptedModel()))
        self.server = MCPServer()
        self.stack.enter_context(patch("mcp.client.streamable_http.streamablehttp_client",
                                       self.server.transport))
        self.config = {"max_tool_calls": 5,
                       "tools": {"enabled_tools": ["auto_web_search"]}}

    def agent(self, model=None):
        model = model or ScriptedModel()
        with patch("agents.task.execution_agent.get_model", return_value=model):
            return ExecutionAgent(model="o4-mini", config=self.config)

    def test_scripted_agent_consumes_search_fetch_followup_and_citations(self):
        model = ScriptedModel()
        agent = self.agent(model)
        with patch("agents.task.execution_agent.get_model", return_value=model):
            result = agent.execute("Find research evidence", {"task": "Research"})
        self.assertNotIn("error", result)
        calls = self.server.calls()
        self.assertEqual([c["name"] for c in calls], ["web_search", "web_fetch", "web_search"])
        self.assertEqual(len({c["arguments"]["session_id"] for c in calls}), 1)
        self.assertIn("Evidence about", str(model.messages[-1]))
        self.assertIn("'tag': '1'", str(model.messages[-1]))
        self.assertEqual(agent.reference_manager.get_reference_list()[0]["url"],
                         "https://example.org/research")
        self.assertTrue(self.server.requests)
        for request in self.server.requests:
            self.assertEqual(str(request.url), parallel_search.MCP_URL)
            self.assertEqual(request.headers["user-agent"], parallel_search.USER_AGENT)
            self.assertNotIn("authorization", request.headers)

    def test_new_and_concurrent_runs_keep_ids_with_shared_manager(self):
        barrier = threading.Barrier(2)
        agents = [self.agent(ScriptedModel(barrier)) for _ in range(2)]
        self.assertIs(agents[0].tool_manager, agents[1].tool_manager)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(agent.execute, "Research", {"task": "Research"})
                       for agent in agents]
            for future in futures:
                future.result(timeout=20)
        ids = [c["arguments"]["session_id"] for c in self.server.calls()]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(sorted(ids.count(s) for s in set(ids)), [3, 3])
        # Reusing the same agent and cached manager for a new run resets identity.
        agents[0].model_instance.barrier = None
        agents[0].execute("New research", {"task": "New research"})
        new_ids = {c["arguments"]["session_id"] for c in self.server.calls()[6:]}
        self.assertEqual(len(new_ids), 1)
        self.assertTrue(new_ids.isdisjoint(ids))

    def test_global_execution_threads_inherit_research_context(self):
        manager = ToolManager(config=self.config)
        executor = GlobalExecutionAgent.__new__(GlobalExecutionAgent)
        executor.max_workers = 2
        executor.logger = manager.logger
        executor._execute_single_node = lambda *a, **kw: manager.call_tool(
            "search_parallel", query="research evidence")
        with search_run():
            manager.call_tool("search_parallel", query="first research query")
            results = executor._execute_nodes_parallel(["a", "b"], {})
            self.assertEqual(set(results), {"a", "b"})
        self.assertEqual(len({c["arguments"]["session_id"] for c in self.server.calls()}), 1)

    def test_workflow_boundary_resets_identity_on_reuse(self):
        manager = ToolManager(config=self.config)
        # Keep the production Workflow.execute boundary, using an empty planned
        # graph so the orchestration needs neither model inference nor a dataset.
        graph = DirectedGraph()
        workflow = Workflow.__new__(Workflow)
        workflow.config = {}
        workflow.global_planner = SimpleNamespace(
            graph=graph, execute=lambda *a, **kw: None)

        def execute(**kwargs):
            manager.call_tool("search_parallel", query="workflow research")
            manager.call_tool("fetch_parallel", url="https://example.org/research")
            return {}

        workflow.global_execution_agent = SimpleNamespace(
            set_execution_graph=lambda graph: None,
            show_all_nodes_info=lambda **kw: graph,
            execute=execute, get_final_answer=lambda: "Answer")
        workflow.execute("First question")
        workflow.execute("Second question")
        ids = [call["arguments"]["session_id"] for call in self.server.calls()]
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[2], ids[3])
        self.assertNotEqual(ids[0], ids[2])

    def test_provider_choices_saved_lists_disabled_and_overrides(self):
        # Existing non-Parallel providers are ancillary to this change. Observe
        # the unchanged legacy loader boundary without their GPU/browser imports.
        selected = []
        def legacy_loader(config):
            selected.extend(config["tools"]["enabled_tools"])
            return []
        module = SimpleNamespace(construct_agent_list=legacy_loader)
        with patch.dict(sys.modules, {"tool_integration": module}):
            for key, category in (("VOLC_SEARCH_API_KEY", "volc_search"),
                                  ("SERPER_API_KEY", "web_search"),
                                  ("TAVILY_API_KEY", "tavily_search")):
                with patch.dict(os.environ, {key: "configured"}):
                    manager = ToolManager(config=self.config)
                    self.assertEqual(manager.list_tools(), [])
                    self.assertEqual(selected.pop(), category)
            manager = ToolManager(config={"tools": {"enabled_tools": ["volc_search"]}})
            self.assertEqual(manager.list_tools(), [])
            self.assertEqual(selected.pop(), "volc_search")
            for enabled in ([], None):
                self.assertEqual(ToolManager(config={"tools": {"enabled_tools": enabled}}).list_tools(), [])
            with patch.dict(os.environ, {"VOLC_SEARCH_API_KEY": "configured"}):
                override = {"tools": {"enabled_tools": ["auto_web_search"],
                                      "web_search_provider": "parallel"}}
                self.assertIn("search_parallel", tools.get_tool_manager(config=override).list_tools())
                self.assertNotIn("search_parallel", tools.get_tool_manager(config=self.config).list_tools())
            for path in DR_ROOT.glob("config_*.yaml"):
                config = yaml.safe_load(path.read_text())
                self.assertIn("search_parallel", ToolManager(config=config).list_tools())
        self.assertEqual(self.server.requests, [])

    def test_permissions_urls_text_results_errors_and_quota_retry(self):
        agent = self.agent()
        with self.assertRaises(ValueError):
            agent._execute_single_tool_call({"name": "search_volc", "arguments": {"query": "x"}})
        for url in ("file:///etc/passwd", "relative", "https://user:pass@example.org"):
            result = agent._execute_single_tool_call({"name": "fetch_parallel", "arguments": {"url": url}})
            self.assertFalse(result["success"])
        self.assertEqual(self.server.requests, [])
        with search_run():
            self.server.error = "Anonymous quota exceeded. Retry later."
            result = agent._execute_single_tool_call({"name": "search_parallel", "arguments": {"query": "research"}})
            self.assertFalse(result["success"])
            self.assertIn("quota exceeded", result["error"])
            self.server.error = None
            self.server.structured = False
            result = agent._execute_single_tool_call({"name": "search_parallel", "arguments": {"query": "research"}})
            self.assertTrue(result["success"])
            self.assertIn("Evidence", str(result))
        calls = self.server.calls()
        self.assertEqual(len(calls), 2)  # No silent retries or provider fallback.
        self.assertEqual(calls[0]["arguments"]["session_id"], calls[1]["arguments"]["session_id"])

    def test_deadline_and_cancellation_release_transport(self):
        self.server.delay = True
        manager = ToolManager(config=self.config)
        with patch.object(parallel_search, "REQUEST_TIMEOUT", 0.05):
            with self.assertRaises(TimeoutError):
                manager.call_tool("search_parallel", query="slow research")

        async def cancel():
            task = asyncio.create_task(manager.get_tool("search_parallel")("cancel research"))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        asyncio.run(cancel())


if __name__ == "__main__":
    unittest.main()
