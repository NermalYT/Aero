"""use_skill: load a skill's full instructions on demand."""
from pathlib import Path

from . import tool
from .. import skills


def _use(ctx, name):
    r = skills.load(name)
    if r[0] is None:
        return {"text": r[1], "error": True}
    sk, body, files = r
    root = Path(sk["path"]).parent
    try:                                 # read_file finds "references/x.md" here too (1.3.1)
        ctx.skill_dirs = [root] + [d for d in (getattr(ctx, "skill_dirs", None) or []) if d != root][:4]
    except AttributeError:
        pass
    # first, not only in the list at the end: models read "references/x.md" against the working folder (seen live)
    text = (f"# Skill: {sk['name']}\nThis skill's folder: {root}\nPaths in these instructions (references/..., "
            f"assets/...) are inside that folder: read {root / 'references' / '<file>'} with read_file, not the "
            f"working folder.\n\n{body[:30000]}")
    if files:
        text += "\n\n## Files in this skill (read them with read_file when the instructions refer to them)\n" + \
                "\n".join(f"- {root / f}" for f in files)
    return text


tool("use_skill", "Load a skill: step-by-step instructions (and helper files) for a specific kind of task. Use it when "
     "the turn context lists a relevant skill or the task clearly matches one.", "skills",
     {"name": {"type": "string", "description": "Skill name"}}, ["name"], summary=lambda a: a.get("name", ""))(_use)
