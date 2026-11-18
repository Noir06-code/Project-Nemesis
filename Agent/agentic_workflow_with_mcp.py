import asyncio
from typing import Annotated
from typing_extensions import TypedDict

from langchain.chat_models import init_chat_model
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
from langchain_mcp_adapters.client import MultiServerMCPClient
#from Tools.tools import tools
from dotenv import load_dotenv

load_dotenv()


class State(TypedDict):
    messages: Annotated[list, add_messages]


class Agent():
    # TODO: I have to add tool rag here and dynamic system prompt skill based in agents
    def __init__(self, model="google_genai:gemini-2.5-flash", system_prompt="",
                 tools=None, mcp_servers=None):

        self.llm = init_chat_model(model)
        self.system_prompt = system_prompt
        self.local_tools = tools or []
        self.mcp_client = MultiServerMCPClient(mcp_servers) if mcp_servers else None
        self.graph = None  # built lazily, because MCP tool loading is async

    async def _build(self):
        # local tools + every tool from every MCP server, in one flat list
        tools = list(self.local_tools)
        if self.mcp_client:
            tools += await self.mcp_client.get_tools()

        self.llm_with_tools = self.llm.bind_tools(tools)

        builder = StateGraph(State)
        builder.add_node("chatbot", self.chatbot)
        builder.add_node("tools", ToolNode(tools))
        builder.add_edge(START, "chatbot")
        builder.add_conditional_edges("chatbot", tools_condition)
        builder.add_edge("tools", "chatbot")
        self.graph = builder.compile()

    async def chatbot(self, state: State):
        messages = state["messages"]
        if self.system_prompt:
            messages = [{"role": "system", "content": self.system_prompt}] + messages
        return {"messages": [await self.llm_with_tools.ainvoke(messages)]}

    async def ainvoke(self, message):
        if self.graph is None:
            await self._build()
        state = await self.graph.ainvoke(
            {"messages": [{"role": "user", "content": message}]}
        )
        return state["messages"][-1].content


async def main():
    agent = Agent(mcp_servers={
        "math": {
            "url": "http://localhost:8001/mcp",
            "transport": "streamable_http",
        },
        "weather": {
            "url": "http://localhost:8002/mcp",
            "transport": "streamable_http",
        },
        "notes": {
            "url": "http://localhost:8003/mcp",
            "transport": "streamable_http",
        },
    })

    print(await agent.ainvoke("What's (3 + 5) x 12, and what's the weather in Dhaka?"))


if __name__ == "__main__":
    asyncio.run(main())