# AgentOS Skills

Skills — Python adapters that connect AgentOS to third-party services
(GitHub, Google, iMessage, Brave, etc.) and expose agent-only tools
(LLM, web search, file system). Written against the Python **Skills
SDK** in the [`sdk-skills/`](../sdk-skills) sibling repo.

[agentos.to](https://agentos.to) · [agentos.to/skills](https://agentos.to/skills/)

## What is AgentOS?

A local operating system for human-AI collaboration, built for agents
first. The engine speaks MCP so any MCP-capable agent (Claude Code,
Cursor, etc.) can use AgentOS as its tool surface. Your data stays on
your machine.

Skills are how capabilities arrive. A skill declares what it
**provides** — `@provides(llm)`, `@provides(web_search)`,
`@provides(file_system)` — and the engine matchmakes requests to the
best available provider. Callers ask for a capability, not a specific
skill.

## What's here

```
agents/         Agent-only tools (code-review, problem-solving, …)
ai/             LLM providers (anthropic, openai, …)
comms/          Messaging (imessage, whatsapp, slack, …)
finance/        Banking, budgeting, payments
hosting/        DNS, deployment (porkbun, vercel, …)
logistics/      Ride-share, delivery, travel
media/          Books, music, video (goodreads, spotify, …)
productivity/   Calendar, tasks, notes
web/            Browsers, search, scraping
```

Each top-level category is flat so the repo doubles as a browse-able
catalog — clone and you immediately see every skill.

## Getting started

```bash
git clone https://github.com/agentos-to/skills
git clone https://github.com/agentos-to/sdk-skills    # sibling repo
cd skills
pip install -e ../sdk-skills                  # ships the validator
git config core.hooksPath ../bin/git-hooks    # pre-commit + code review
```

Useful commands:

```bash
agent-sdk validate                  # lint every skill in this repo
agent-sdk validate exa              # single skill
agent-sdk validate --sandbox        # only the banned-import sandbox check
agent-sdk new-skill my-skill        # scaffold a new skill
agent-sdk shapes                    # list available shapes
```

Full authoring guide at
[agentos.to/skills](https://agentos.to/skills/).

## Sibling repos

| Repo                                                       | Lang         | What |
| ---------------------------------------------------------- | ------------ | ---- |
| [`core`](https://github.com/agentos-to/core)               | Rust         | The engine, CLI, MCP server |
| [`docs`](https://github.com/agentos-to/docs)               | Astro + YAML | Docs + shapes (ontology) — deploys to [agentos.to](https://agentos.to) |
| **`skills`** (this repo)                                   | Python       | Skills — adapters for third-party services |
| [`sdk-skills`](https://github.com/agentos-to/sdk-skills)   | Python       | Skills SDK — the `agentos` package |
| [`apps`](https://github.com/agentos-to/apps)               | TypeScript   | Apps + React components |
| [`sdk-apps`](https://github.com/agentos-to/sdk-apps)       | TypeScript   | Apps SDK — components + generated TS shapes |

## Contributing

Anyone can contribute. Found a bug? Want a new skill?
[Open an issue](https://github.com/agentos-to/skills/issues) or a PR.

## License

MIT — see [LICENSE](LICENSE). By contributing you grant AgentOS the
right to use your contributions in official releases, including
commercial offerings. Your code stays open forever.
