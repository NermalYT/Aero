"""run_subagent: hand a self-contained part of the task to a fresh copy of the local model.

The agent loop runs it (agent.run_subagent), because a subagent streams its own steps to the UI; this module only
registers the schema the model sees."""
from . import tool


@tool("run_subagent",
      "Hand one self-contained part of your task to a subagent: a fresh copy of you with its own empty context and "
      "the same tools. Use it for side work that would flood your context (reading many files, a search, checking "
      "a long log) or for independent parts of a bigger job. It cannot see this chat, so the task must be a "
      "complete brief: the goal, the exact paths, apps or URLs, and what to report back. It works until done, then "
      "you get only its final report. Subagents run one at a time.",
      "agents",
      {"name": {"type": "string", "description": "A 1-3 word job title for what it does, e.g. 'Log Reader', "
                                                 "'Photo Counter', 'Test Fixer'"},
       "task": {"type": "string", "description": "The complete brief: goal, where to look, what to report"}},
      ["name", "task"], summary=lambda a: f"{a.get('name', '')}: {a.get('task', '')}"[:160])
def run_subagent(ctx, name="", task=""):
    return {"text": "Only the local model's agent loop can start subagents.", "error": True}
