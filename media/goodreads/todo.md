---
name: goodreads
type: skill-todo
---

# goodreads/ — TODO + compaction-survival notes

Living doc. If you're an agent picking this skill up after compaction,
**read this first**. It captures what the previous session learned so
you don't have to relearn it.

## Known bugs

### Silent login-redirect on `/review/list/`

`list_books` / `list_reviews` / `list_shelf_books` all return `[]` when
Goodreads redirects to `/user/sign_in`. The `/review/list/` endpoint
requires stronger auth than the homepage — `check_session` succeeds
but `/review/list/` serves the sign-in page with 200 OK. `_require_login`
only raises on page 1 of the auto-paginate loop, after parsing. When
page 1 is the sign-in HTML (no `bookalike` rows, no next-page), the
loop silently `break`s with empty results.

**Fix both sides.**

1. Move `_require_login` before the parse, and run it every page,
   not only page 1 — so a mid-pagination session death surfaces as
   `SESSION_EXPIRED` instead of a truncated list.
2. Figure out why `/review/list/` needs a different cookie set than
   `/`. Probably missing `_session_id2` or an HttpOnly cookie the
   Brave decrypter isn't extracting. Check `Set-Cookie` from a full
   browser session vs what the cookie provider returns.

**Repro.** `list_shelves` reports 72 read + 8 currently-reading + 221
to-read; `list_books` returns 0 for every shelf. HTML dumped to
/tmp during debug showed `<title>Sign in</title>`.

## Open work

This skill's remaining friction is now captured as engine-level projects
in `core/_projects/p2/`:

- **accounts-by-email** — replace numeric `26631647` + `default`
  fallback with `goodreads@contini.co`.
- **test-decorator** — move `test:` YAML into Python `@test`. Unblocks
  `from_account()` and kills the string-key drift that broke this skill.
- **connections-as-http-clients** — move `waf/mode/accept` off call
  sites into a `client:` profile on the connection. Deletes the
  conditional in `_fetch_url`.
- **python-first-skill-surface** — umbrella: readme becomes prose only,
  all declarative surface moves to Python decorators.
- **unified-quality-runner** — merge `quality run` + `run_tests.py` into
  one command, one JSONL row.

Goodreads is pilot #1 for every one of those.

### `@test` decorator (snapshot of intent)

**Today.** Tests in `readme.md` frontmatter. YAML key must match the
Python function name exactly — the `run_` rename bug.

**Target.**

```python
@test(user_id=from_account())
@returns("person")
@connection("graphql")
async def get_person(*, user_id, **params): ...
```

AST-read by the SDK (same path as `@returns`, `@provides`). No
runtime import — engine parses the source as text. Multi-case form:

```python
@test.cases(
    {"params": {"book_id": "4934"}},
    {"params": {"book_id": "5107"}},
)
```

Skip form:

```python
@test.skip(reason="destructive — creates a review")
```

**Migration plan.** Pilot on Goodreads + macos-control (two existing
skills with `test:` blocks). Runner learns a second source: prefer
Python `@test` if present, fall back to readme `test:` for any skill
not yet migrated. Lets skills migrate one at a time.

**Why deferred.** Goodreads fix came first because the engine
already worked for the readme-based form; needed to stop the
bleeding before changing the declaration site.

### 3. Login/signup flow from empty state

Joe raised this. **Recommendation: don't build it.** Goodreads
sign-in has CloudFront WAF + invisible reCAPTCHA; building password
login costs weeks. Brave cookie sync already solves the empty-state
problem — user signs into Goodreads in their browser, engine reads
the cookies, done. Keep "signup inside AgentOS" as a long-term vision
note, not a near-term TODO.

## Compaction-survival notes — what I wish I'd known

### Engine dispatch is verbatim

The Rust engine parses skill `.py` files as text
(`core/crates/core/src/skills/python_ast.rs`). A tool's **name is
the Python function name exactly** — no prefix stripping, no
mangling, no magic. If your function is `async def get_book(...)` and
you call with `tool: "getBook"`, it fails with `Tool 'getBook' not
found. Available: get_book, ...`.

Corollary: **`run_` prefixes are noise.** The skill name is already
the namespace. Name your functions `get_book`, not `run_get_book`.

### Only `@returns`-decorated functions become tools

`python_ast.rs:128` — parser only promotes a function to an operation
when it sees `@returns(...)` among the pending decorators. No
`@returns` = not a tool, even if the name looks right. This is why
underscored helpers (`_get_public_book`, `_fetch_url`) aren't
exposed — they lack the decorator.

### Everything I/O is async

`http.get`, `http.post`, `http.put`, `http.delete` — all async.
Every helper in your call chain that eventually touches `http` must
be `async def`. Calling a coroutine without `await` returns a
coroutine object; the first `.get("ok")` on it raises
`AttributeError: 'coroutine' object has no attribute 'get'`. That
exact pattern is how this skill was broken for weeks.

**`time.sleep(x)` inside an `async def` is a real bug** — blocks the
event loop. Use `await asyncio.sleep(x)`. `agent-sdk validate`
catches this.

### Two accounts = "Multiple accounts. Specify account."

If a skill has more than one logged-in account, the engine refuses to
pick one. Every call must include `account: "<name>"`. When testing
today, pick by looking at `agentos call accounts '{"skill":"<id>"}'`.

### No parallel testing convention

`_quality/charter.md` forbids inventing one. Tests live in the same
surface used by `agent-sdk validate`. Today that surface is the
readme `test:` YAML. When the `@test` decorator lands, the surface
becomes Python-AST — but it stays a single surface.

### CLAUDE.md golden rules that keep biting

- **Never run `cargo build` directly.** Use `./dev.sh build`. A hook
  blocks it.
- **Full-path the engine binary**:
  `/Users/joe/dev/agentos/core/target/debug/agentos`. The `agentos`
  on PATH may be a stale build.
- **Skill code edits don't need a rebuild.** Python + readme
  frontmatter are re-read on every dispatch. Only Rust changes
  require `./dev.sh build` + `./dev.sh restart`.
- **Delete legacy on sight.** No `# deprecated` breadcrumbs, no
  `TODO remove` shims. Pre-launch means no users, no migration path.
- **Test against real services, not mocks.** The sweep hits
  production Goodreads. That's the bar.

### Shape-native return dicts

Skills return dicts whose keys match a shape in `docs/shapes/*.yaml`.
`agent-sdk validate` AST-checks the returns against the declared
shape. Don't invent keys; pick an existing shape
(`docs/src/content/docs/shapes.md`). 95% of what a skill needs is
already shaped.

### The AppSync-key extractor is self-healing

The public Goodreads GraphQL backend rotates its `da2-…` key on each
redeploy. This skill reads the key fresh from Goodreads' own
`_app-<hash>.js` bundle on every call — see `_discover_from_bundle`.
If the regexes (`APP_BUNDLE_RE`, `APPSYNC_ENDPOINT_RE`) ever stop
matching, don't hardcode a fallback — fix the extractor. That's the
contract.

## Doing the sweep after edits

```bash
# engine up?
/Users/joe/dev/agentos/core/target/debug/agentos call --list | head

# validate
skills/agent-sdk validate skills/media/goodreads

# one tool
/Users/joe/dev/agentos/core/target/debug/agentos call run \
  '{"skill":"goodreads","tool":"get_book","params":{"book_id":"4934"},"account":"26631647"}'

# sweep
cd _quality && bin/quality run
bin/quality open
```
