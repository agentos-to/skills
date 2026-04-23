# ABP auth — reverse-engineering notes

Source of truth for the Cognito + portal handshake on
`boulderingproject.portal.approach.app`. Keep this current while the
session model is still being figured out; once the `login` tool's
Python replay matches the browser byte-for-byte, delete everything
below except a one-liner pointing at the code.

## What's confirmed

**The portal frontend is a CloudFront-fronted S3 static SPA.**
`curl -I https://boulderingproject.portal.approach.app/login` → 403
to a bare User-Agent, CloudFront + `server: AmazonS3` headers. The
login page is JS that authenticates client-side against AWS Cognito.

**Cognito `USER_PASSWORD_AUTH` works.** `_cognito_initiate_auth(email,
password)` has been the authed path for months — POSTs to
`https://cognito-idp.us-east-1.amazonaws.com/` with
`AuthFlow: USER_PASSWORD_AUTH`, returns
`AuthenticationResult.{IdToken, AccessToken, RefreshToken, ...}`.
Cognito pool id + client id are discovered at runtime from the
portal's `app-*.js` bundle.

**Portal API requests use the IdToken as a bearer token.** Every
authed endpoint under `portal.api.prod.tilefive.com` accepts
`Authorization: <IdToken>` (raw, no `Bearer ` prefix — see
`_portal_headers` in `abp.py`). Production traces confirm 200 OK.

## Open question

Does the portal frontend establish anything on
`.approach.app` after Cognito returns the IdToken (e.g. a session
cookie for SSR)? Given the static-SPA shape there's no backend to
set such a cookie — the token almost certainly lives in JS
memory/localStorage and rides `Authorization` on every API call.

**If the current bearer-token flow works end-to-end when wired up,
delete this file.** The code is the source of truth; the note exists
only while there's a gap between what the skill does and what the
browser does.

## If a capture is needed

Use `core/bin/browse-capture.py` (CDP to a real Chrome/Brave) to
record a real login session against the portal. Capture:

- The Cognito `InitiateAuth` POST (already implemented).
- Every subsequent portal API request until a successful authed
  render — in particular, whether `Set-Cookie` lands anywhere on
  `.approach.app` or whether `Authorization` is the only auth
  carrier.
- Whatever Cognito's `InitiateAuth` returns beyond `IdToken`
  (token ttl, refresh token, session metadata the portal UI uses).

If the capture shows anything the skill isn't already replaying,
wire it into `login` / `_cognito_initiate_auth`, then delete the
capture artifacts and this file.
