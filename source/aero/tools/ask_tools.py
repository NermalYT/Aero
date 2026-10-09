"""ask_user and get_answer: questions to the user that don't stop the rest of the task (clarifications.py).

The agent loop runs them (agent.py), because a question shows a card in the chat and waiting needs Stop; this module
only registers the schemas the model sees."""
from . import tool


@tool("ask_user", "Ask the user one short question while you keep working. Use it only when a wrong guess would "
      "matter (which account, which file, which of several equally good options). It returns at once with a "
      "question id: continue with everything that doesn't depend on the answer, then call get_answer(id) when you "
      "need it. Give choices when there are a few clear options.", "ask",
      {"question": {"type": "string"},
       "choices": {"type": "array", "items": {"type": "string"}, "description": "Options to pick from (max 8)"},
       "free_text": {"type": "boolean", "description": "Allow an answer that isn't one of the choices (default true)"},
       "kind": {"type": "string", "enum": ["text", "choice", "yes_no", "number", "date"]}},
      ["question"], summary=lambda a: a.get("question", "")[:120])
def ask_user(ctx, question="", choices=None, free_text=True, kind="text"):
    return {"text": "Only the agent loop can ask the user.", "error": True}


@tool("get_answer", "Get the user's answer to a question you asked with ask_user. Waits until they answer (or "
      "returns at once when they already did).", "ask",
      {"id": {"type": "string", "description": "The question id from ask_user"},
       "wait": {"type": "boolean", "description": "Wait for the answer (default true); false just checks"}},
      ["id"], summary=lambda a: a.get("id", ""))
def get_answer(ctx, id="", wait=True):
    return {"text": "Only the agent loop can wait for an answer.", "error": True}
