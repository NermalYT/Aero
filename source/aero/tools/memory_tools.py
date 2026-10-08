"""Long-term memory tools: the model saves lasting facts and searches earlier chats."""
import time

from . import tool
from .. import memory


def _when(t):
    return time.strftime("%Y-%m-%d", time.localtime(t or 0))


@tool("remember", "Save a lasting fact to long-term memory so every future chat knows it: who the user is, their "
      "setup, preferences, ongoing projects, decisions. One short self-contained sentence per call. Never save "
      "passwords, keys or tokens.", "memory",
      {"text": {"type": "string", "description": "The fact, e.g. 'The user's main GPU is an RTX 4070 12 GB.'"},
       "kind": {"type": "string", "enum": ["user", "preference", "project", "environment", "other"]}},
      ["text"], summary=lambda a: a.get("text", ""))
def remember(ctx, text, kind="other"):
    if memory.SECRET_RE.search(text or ""):
        return {"text": "Not saved: that looks like a secret (password, key or token).", "error": True}
    f, how = memory.add_fact(text, kind, source=ctx.chat_id)
    return f"{'Saved' if how == 'added' else 'Updated an existing memory'} (id {f['id']}): {f['text']}"


@tool("recall", "Search long-term memory and earlier chats (facts, chat summaries and past messages). Use it when "
      "the user refers to something from before that isn't in this chat.", "memory",
      {"query": {"type": "string"}, "max_results": {"type": "integer"}}, ["query"],
      summary=lambda a: a.get("query", ""))
def recall(ctx, query, max_results=8):
    hits = memory.search(query, int(max_results or 8))
    if not hits:
        return "Nothing in memory or earlier chats matches that."
    out = []
    for h in hits:
        if h["type"] == "fact":
            out.append(f"- [memory {h['id']}, {_when(h['when'])}] {h['text']}")
        elif h["type"] == "chat_summary":
            out.append(f"- [summary of chat \"{h['title']}\", {_when(h['when'])}] {h['text'][:1500]}")
        else:
            out.append(f"- [{h['role']} in chat \"{h['title']}\", {_when(h['when'])}] ...{h['text']}...")
    return "\n".join(out)


@tool("forget", "Delete a memory by its id (shown by recall) when it is wrong or the user asks to forget it.",
      "memory", {"id": {"type": "string"}}, ["id"], summary=lambda a: a.get("id", ""))
def forget(ctx, id):
    return "Deleted." if memory.delete_fact(id) else {"text": f"No memory with id {id}.", "error": True}
