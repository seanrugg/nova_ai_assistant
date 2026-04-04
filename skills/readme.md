# Nova Skills

Skills are optional modules that extend Nova's capabilities for specific contexts.
They are loaded at launch and injected into Nova's system prompt, shaping how she
behaves during that session.

Skills are designed to be **shareable and community-contributed**. If you build a
skill that works well for your family, consider submitting a pull request.

---

## Using Skills

Load one or more skills when launching Nova:

```bash
./nova_launch.sh --user devyn --skills education
./nova_launch.sh --user jihan --skills homework,fitness
./nova_launch.sh --user sean  --skills news,mindfulness
```

Skills are comma-separated. They are applied in the order listed.

---

## Available Skills

| Skill | File | Personas | Description |
|---|---|---|---|
| Education | `skill_education.py` | child | Storytelling, letter games, animal facts, phonics |
| Homework | `skill_homework.py` | child, teen | Study help, quiz games, subject tutoring |
| Fitness | `skill_fitness.py` | teen, adult | Workout coaching, habit encouragement |
| Cookbook | `skill_cookbook.py` | adult | Recipe suggestions, meal planning |
| Mindfulness | `skill_mindfulness.py` | teen, adult | Breathing exercises, reflection prompts |
| News | `skill_news.py` | adult | Current events discussion, conversation starters |

---

## Writing a Skill

A skill is a single Python file in the `skills/` directory named `skill_<name>.py`.

### Minimal skill

```python
SKILL_NAME = "example"

SKILL_PROMPT = """
EXAMPLE SKILL — ACTIVE

Describe here how Nova should behave differently with this skill loaded.
This text is injected directly into Nova's system prompt.
Be specific and directive — tell Nova what to do, not just what the skill is.
"""
```

### Full skill with persona filtering and activities

```python
"""
Nova Skill: Example
Invocation: python3 nova.py --user name --skills example
"""

SKILL_NAME = "example"

# Optional: limit this skill to specific personas.
# Valid values: "child", "teen", "adult"
# If omitted, the skill loads for all personas.
SKILL_PERSONAS = ["teen", "adult"]

SKILL_PROMPT = """
EXAMPLE SKILL — ACTIVE

Tell Nova exactly how to behave with this skill active.

Guidelines:
- Be specific and actionable
- Write in the imperative ("Do this", "Offer that")
- Keep it focused — one skill, one purpose
- Reference the user's persona if relevant
- Don't repeat instructions already in the base persona
"""

# Optional: structured activity definitions for reference or future UI use.
# Not currently used by nova.py but useful for documentation and tooling.
SKILL_ACTIVITIES = [
    {
        "name": "activity_name",
        "description": "What this activity does",
        "trigger_phrases": ["phrases that might invoke this naturally"],
    },
]
```

---

## Skill Design Principles

**Follow the child's lead, don't drive the session.**
Skills should enrich natural conversation, not turn Nova into a rigid tutor or coach.
The best skills are ones where the user doesn't notice the skill is loaded — they just
notice Nova is unusually good at something.

**One skill, one purpose.**
Keep each skill tightly focused. A skill that tries to do homework help, fitness
coaching, and meal planning is a skill that does none of them well.

**Persona awareness.**
A skill written for a 4-year-old should declare `SKILL_PERSONAS = ["child"]`.
This prevents it loading for teenagers or adults where the tone would be wrong.
If your skill works for everyone, omit `SKILL_PERSONAS` entirely.

**Prompt injection is additive.**
Your `SKILL_PROMPT` is appended to Nova's existing system prompt — it does not
replace it. Nova's identity, family knowledge, and persona style all remain active.
Write your skill prompt assuming Nova already knows who she's talking to.

**Keep prompts concise.**
The system prompt has a context window limit. Skill prompts should be focused and
tight — a few paragraphs at most. If your prompt is getting long, split it into
two skills.

**Test with a real conversation.**
Launch Nova with your skill loaded and have a natural conversation. Does she use
the skill organically? Does it feel forced? Adjust the prompt until it feels seamless.

---

## Skill File Naming

Skills must be named `skill_<name>.py` and placed in the `skills/` directory.

```
skills/
  skill_education.py     ✅
  skill_homework.py      ✅
  my_skill.py            ❌  (won't be found — must start with skill_)
  education.py           ❌  (won't be found — must start with skill_)
```

---

## Contributing a Skill

1. Fork the repository
2. Create your skill file in `skills/`
3. Test it with at least one persona
4. Add it to the table in this README
5. Submit a pull request with a brief description of what the skill does and who it's for

Please do not include any personal or family-specific information in contributed skills.
Skills should be generic and useful to any household.
