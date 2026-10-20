"""Anonymous Parallel Search MCP tools for the Deep Research dispatcher."""

import asyncio
import json
import os
from datetime import timedelta
from urllib.parse import urlsplit

from utils.search_context import search_session_id


MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = "InternAgent/0.1.0 (+https://github.com/InternScience/InternAgent)"
REQUEST_TIMEOUT = 60


def resolve_search_provider(config):
    """Resolve only the automatic category; explicit tool categories stay intact."""
    provider = config.get("tools", {}).get("web_search_provider", "auto")
    if provider not in ("auto", "parallel", "volc", "serper", "tavily"):
        raise ValueError(f"Unknown web_search_provider: {provider}")
    if provider != "auto":
        return provider
    for name, key in (("volc", "VOLC_SEARCH_API_KEY"),
                      ("serper", "SERPER_API_KEY"),
                      ("tavily", "TAVILY_API_KEY")):
        if os.getenv(key):
            return name
    return "parallel"


def _http_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Expected an absolute HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs must not contain credentials")
    return url


async def _request(tool_name, arguments):
    # Lazy imports keep disabled search usable without loading the MCP SDK.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    arguments = {**arguments, "session_id": search_session_id()}
    # The deadline covers discovery, invocation and transport cleanup. Cancellation
    # propagates to the SDK; neither failures nor quotas trigger provider fallback.
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            async with streamablehttp_client(
                MCP_URL, headers={"User-Agent": USER_AGENT},
                timeout=timedelta(seconds=REQUEST_TIMEOUT),
            ) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if tool_name not in {tool.name for tool in tools.tools}:
                        raise RuntimeError(f"Parallel MCP does not advertise {tool_name}")
                    result = await session.call_tool(tool_name, arguments)
                    if result.isError:
                        message = "\n".join(
                            item.text for item in result.content if item.type == "text"
                        )
                        raise RuntimeError(f"Parallel {tool_name} failed: {message}")
                    data = result.structuredContent
                    if data is None:
                        text = "\n".join(
                            item.text for item in result.content if item.type == "text"
                        )
                        data = json.loads(text)
                    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
                        raise ValueError("Invalid Parallel MCP result")
                    # Fetch can return partial results; surface per-URL failures too.
                    if data.get("errors"):
                        raise RuntimeError(f"Parallel fetch errors: {data['errors']}")
                    return data["results"]
    except ExceptionGroup as group:
        def messages(error):
            if isinstance(error, ExceptionGroup):
                return "; ".join(messages(child) for child in error.exceptions)
            return str(error) or type(error).__name__
        raise RuntimeError(f"Parallel MCP failed: {messages(group)}") from group


def _results(items):
    results = []
    for item in items:
        excerpts = item.get("excerpts") or []
        content = "\n\n".join(excerpts)
        results.append({
            "result_id": len(results) + 1,
            "title": item.get("title") or item["url"],
            "url": _http_url(item["url"]),
            "description": content,
            "content": item.get("full_content") or content,
        })
    return results


async def search_parallel(query: str, max_results: int = 5):
    """Search the web with free, keyless Parallel Search MCP (Fast mode).

    Args:
        query (str): Focused research question or keyword query.
        max_results (int): Maximum number of sources to return, from 1 to 20.
    """
    if not query.strip():
        raise ValueError("Search query must not be blank")
    if not 1 <= max_results <= 20:
        raise ValueError("max_results must be from 1 to 20")
    items = await _request("web_search", {
        "objective": query, "search_queries": [query],
    })
    return _results(items[:max_results])


async def fetch_parallel(url: str, query: str = ""):
    """Fetch source-page excerpts with free, keyless Parallel Search MCP.

    Args:
        url (str): Absolute HTTP or HTTPS source URL.
        query (str): Optional question to focus the excerpts, up to 200 characters.
    """
    if len(query) > 200:
        raise ValueError("Fetch query must be at most 200 characters")
    arguments = {"urls": [_http_url(url)]}
    if query:
        arguments["objective"] = query
    return _results(await _request("web_fetch", arguments))
