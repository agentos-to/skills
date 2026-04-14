---
id: facebook
capabilities:
  - http
  - shell
name: Facebook
description: Query public Facebook group information without login
color: "#106BFF"
website: "https://facebook.com"
privacy_url: "https://www.facebook.com/privacy/policy"
terms_url: "https://www.facebook.com/legal/terms"
---

# Facebook

Query public Facebook group information without requiring login.

## How It Works

Uses two methods that work without authentication:

### 1. Get Group Metadata (curl)

Public og meta tags are accessible via simple HTTP request:

```bash
curl -s -L -H "User-Agent: Mozilla/5.0" "https://www.facebook.com/groups/GROUP_NAME/" 2>/dev/null
```

**Returns:**
- **Group ID**: From `fb://group/520427849972613` in `al:ios:url` or `al:android:url`
- **Group name**: `og:title` (e.g., "Becoming a Portuguese Citizen | Facebook")
- **Description**: `og:description` (group's about text)

### 2. Get Member Count (Chromium headless)

Member count is loaded via JavaScript, so need headless browser:

```bash
/Applications/Chromium.app/Contents/MacOS/Chromium --headless --dump-dom "https://www.facebook.com/groups/GROUP_NAME/" 2>/dev/null | grep -oE '[0-9,.]+K?\s*members?' | head -1
```

**Returns:** Member count like `2.3K members` or `78,000 members`

## Why This Works

- **curl** can get public og meta tags (title, description, group ID) without login
- **Chromium --dump-dom** renders JavaScript and dumps the final DOM, which includes the dynamically-loaded member count
- Regular curl fails for member count because Facebook loads it via JavaScript

## Implementation Notes

- Use `command` executor with bash script
- Parse og meta tags with grep/sed
- Chromium headless is slower (~2-3s) but required for member count
- Could add `include_members: false` param to skip the slow headless call
- Group must be public for this to work
- Consider caching results since group info doesn't change often

## Examples

```bash
# Portuguese citizenship group
POST /api/adapters/facebook/group.get
{"group": "becomingaportuguesecitizen"}
# → { id: "...", name: "Becoming a Portuguese Citizen", member_count: "2.3K", ... }

# Italian jure sanguinis group (by ID)
POST /api/adapters/facebook/group.get
{"group": "23386646249"}
# → { id: "23386646249", name: "...", member_count: "78,000", ... }
```

## Future Extensions

- `group.search`: Search for groups by keyword (would need different approach)
- `post.list`: Get recent public posts from a group (may require login)
- Authenticated actions via Playwright (like Instagram connector)

## References

- Facebook Graph API — Groups: <https://developers.facebook.com/docs/graph-api/reference/group>
- mbasic.facebook.com — the text-only mobile variant used by scrapers
