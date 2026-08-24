"""Agent: diagnose cause and propose one action per eligible account. Only produces proposals.

The LLM proposes; a deterministic policy validates. This module never executes, never creates a
payment link, never sends. langgraph/langchain imports are confined to graph.py and nodes.py.
"""
