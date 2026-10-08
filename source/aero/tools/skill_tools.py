"""use_skill: load a skill's full instructions on demand."""
from pathlib import Path

from . import tool
from .. import skills


def _use(ctx, name):
    r = skills.load(name)
    if r[0] is None:
        return {"text": r[1], "error": True}
    sk, body, files = r
    text = f"# Skill: {sk['name']}\n(from {sk['path']})\n\n{body[:30000]}"
    if files:
        root = Path(sk["path"]).parent
        text += "\n\n## Files in this skill (read them with read_file when the instructions refer to them)\n" + \
                "\n".join(f"- {root / f}" for f in files)
    return text


tool("use_skill", "Load a skill: step-by-step instructions (and helper files) for a specific kind of task. Use it when "
     "the turn context lists a relevant skill or the task clearly matches one.", "skills",
     {"name": {"type": "string", "description": "Skill name"}}, ["name"], summary=lambda a: a.get("name", ""))(_use)
