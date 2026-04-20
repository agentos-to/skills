---
id: google-calendar
capabilities:
  - http
name: Google Calendar
description: "Read, create, update, and delete Google Calendar events — replaces apple-calendar with Google API + OAuth"
color: "#4285F4"
website: "https://calendar.google.com"
privacy_url: "https://policies.google.com/privacy"

connections:
  api:
    base_url: https://www.googleapis.com/calendar/v3
    auth:
      type: oauth
      service: google
      scopes:
      - https://www.googleapis.com/auth/calendar.events

product:
  name: Google Calendar
  website: https://calendar.google.com
  developer: Google LLC

test:
  list_calendars: {}
  list_events:
    params:
      days: 7
  # Writes — skip.
  create_event:
    skip: true
  update_event:
    skip: true
  delete_event:
    skip: true
  get_event:
    skip: true
  search_events:
    skip: true
---
