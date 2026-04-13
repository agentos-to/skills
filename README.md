# AgentOS Skills

Community skills and themes for [AgentOS](https://github.com/agentos/core).

---

## What is AgentOS?

**AgentOS is the semantic layer between AI assistants and your digital life.**

Your tasks are in Todoist. Your calendar is in Google. Your messages are split across iMessage, WhatsApp, Slack. Each service is a walled garden. AgentOS gives AI assistants a unified way to access all your services through a universal entity model.

---

## What's Here

```
skills/            Skills — YAML configs + Python helpers
themes/            UI themes
```

Browse `skills/` for all available skills.

---

## Documentation

All developer documentation lives in the [agentOS docs repo](https://github.com/agentos/docs):

| What | Where |
|------|-------|
| **Skill development guide** | `docs/src/content/docs/skills.md` |
| **Shapes, connections, auth** | `docs/src/content/docs/` |
| **Reverse engineering** | `docs/src/content/docs/reverse-engineering/` |
| **Quick reference** | `agentos.to/skills.md` |

---

## Contributing

**Anyone can contribute.** Found a bug? Want a new skill? Have an idea? [Open an issue](https://github.com/agentos/skills/issues).

```bash
git clone https://github.com/agentos/skills
cd skills
# Install the SDK once — it ships the validator used by pre-commit
pip install -e ./_sdk
# Arm the pre-commit hook (runs validator + code review on every commit)
git config core.hooksPath ../bin/git-hooks
```

Useful commands:

```bash
agent-sdk validate                  # lint every skill in this repo
agent-sdk validate exa              # single skill
agent-sdk validate --sandbox        # only the banned-import sandbox check
agent-sdk new-skill my-skill        # scaffold a new skill
agent-sdk shapes                    # list available shapes
```

---

## License

**MIT** — see [LICENSE](LICENSE).

By contributing, you grant AgentOS the right to use your contributions in official releases, including commercial offerings. Your code stays open forever.
