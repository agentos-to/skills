from agentos import client, connection, provides, returns, timeout
from agentos.tools import llm


connection(
    'api',
    description='Claude API — inference via the Messages API',
    base_url='https://api.anthropic.com/v1',
    auth={'type': 'api_key', 'header': {'x-api-key': '.auth.key'}},
    label='API Key',
    help_url='https://console.anthropic.com/settings/keys')

connection(
    'code',
    description="Claude Code — local CLI, uses the user's existing auth (no API key)")

connection(
    'web',
    description='claude.ai — web chat history via session cookies',
    client='fetch',
    auth={'type': 'cookies', 'domain': '.claude.ai', 'names': ['sessionKey'], 'account': {'check': 'check_session'}, 'login': {'account_prompt': 'What email do you use for claude.ai?', 'phases': [{'name': 'request_login', 'description': 'Submit email on the Claude login page to trigger a magic link email', 'steps': [{'action': 'goto', 'url': 'https://claude.ai/login'}, {'action': 'fill', 'selector': 'input[type=email]', 'value': '${ACCOUNT}'}, {'action': 'click', 'selector': 'button[type=submit]'}], 'returns_to_agent': "Magic link requested. Check the user's email for a message from Anthropic\ncontaining a claude.ai/magic-link URL. Search mail or the graph for that message,\nor ask the user to paste the link.\n"}, {'name': 'complete_login', 'description': 'Navigate to the magic link URL to complete authentication', 'requires': ['magic_link'], 'steps': [{'action': 'goto', 'url': '${MAGIC_LINK}'}, {'action': 'wait', 'url_contains': '/new'}], 'returns_to_agent': 'Login complete. The sessionKey cookie is now in the browser.\nCookie provider matchmaking will extract it automatically on the next API call.\n'}]}})


API_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


def _headers(params):
    key = params.get("auth", {}).get("key", "")
    return {"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION}


_ANTHROPIC = {"shape": "organization", "name": "Anthropic", "url": "https://anthropic.com"}


def _map_model(m: dict) -> dict:
    return {
        "id": m.get("id"),
        "name": m.get("display_name"),
        "at": _ANTHROPIC,
        "published": m.get("created_at"),
        "modelType": "llm",
    }


def _to_anthropic_msg(msg: dict) -> dict:
    if msg.get("role") == "assistant" and msg.get("tool_calls"):
        content = []
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        for tc in msg["tool_calls"]:
            content.append({"type": "tool_use", "id": tc["id"],
                            "name": tc["name"], "input": tc["input"]})
        return {"role": "assistant", "content": content}
    if msg.get("role") == "tool":
        return {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": msg["tool_call_id"],
            "content": msg["content"],
        }]}
    return msg


@returns("model[]")
@connection("api")
async def list_models(**params) -> list:
    """List available Claude models from Anthropic"""
    resp = await client.get(f"{API_BASE}/models",
                    params={"limit": "1000"}, headers=_headers(params))
    return [_map_model(m) for m in (resp["json"] or {}).get("data", [])]


@provides(llm)
@returns({"content": "string", "tool_calls": "array", "stop_reason": "string", "usage": "object"})
@connection("api")
@timeout(120)
async def chat(*, model: str, messages: list, tools: list = None,
         max_tokens: int = 4096, temperature: float = 0,
         system: str = None, **params) -> dict:
    """Send a chat completion request to Claude (Claude API Messages).

        Args:
            model: Model ID — resolved via the graph (list_models). No hardcoded aliases.
            messages: Array of message objects with role and content
            tools: Optional array of tool definitions for function calling
            max_tokens: Maximum tokens to generate
            temperature: Sampling temperature (0 = deterministic for agents)
            system: Optional system prompt
        """
    body = {
        "model": model,
        "messages": [_to_anthropic_msg(m) for m in messages],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if tools:
        body["tools"] = tools
    if system:
        body["system"] = system
    resp = await client.post(f"{API_BASE}/messages",
                     json=body, headers=_headers(params))
    data = resp["json"]
    blocks = data.get("content", [])
    return {
        "content": next((b["text"] for b in blocks if b.get("type") == "text"), None),
        "tool_calls": [{"id": b["id"], "name": b["name"], "input": b["input"]}
                       for b in blocks if b.get("type") == "tool_use"],
        "stop_reason": data.get("stop_reason"),
        "usage": data.get("usage"),
    }
