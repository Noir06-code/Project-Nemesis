from langchain.chat_models import init_chat_model
from typing import Annotated
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode, tools_condition
#from Tools.tools import tools
from dotenv import load_dotenv

load_dotenv()

class State(TypedDict):
    messages: Annotated[list, add_messages]


class Agent():
    # TODO: I have to add tool rag here and dynamic system prompt skill based in agents
    def __init__(self, model="google_genai:gemini-2.5-flash", system_prompt="", tools=[]):

        self.llm = init_chat_model(model)
        self.llm_with_tools=self.llm.bind_tools(tools)
        chatbot=self.chatbot
        builder = StateGraph(State)
        builder.add_node(chatbot)
        builder.add_node("tools", ToolNode(tools))
        builder.add_edge(START, "chatbot")
        builder.add_conditional_edges("chatbot", tools_condition)
        builder.add_edge("tools", "chatbot")
        self.graph = builder.compile()
    
    def chatbot(self,state: State):
        return {"messages": [self.llm_with_tools.invoke(state["messages"])]}
    
    
    
    def invoke(self, message):
        state = self.graph.invoke({"messages": [{"role": "user", "content": message}]})
        return state["messages"][-1].content