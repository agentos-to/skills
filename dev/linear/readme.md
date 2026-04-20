---
id: linear
capabilities:
  - http
name: Linear
description: Project management for engineering teams
color: "#636FD3"
website: "https://linear.app"
privacy_url: "https://linear.app/privacy"
terms_url: "https://linear.app/terms"

connections:
  api:
    base_url: https://api.linear.app/graphql
    auth:
      type: api_key
      header:
        Authorization: .auth.key
    label: API Key
    help_url: https://linear.app/settings/api

test:
  whoami: {}
  list_projects: {}
  get_organization: {}
  get_teams: {}
  get_workflow_states: {}
  list_tasks:
    params:
      limit: 5
  # Writes / destructive ops — skip by default.
  create_task:
    skip: true
  update_task:
    skip: true
  delete_task:
    skip: true
  setup:
    skip: true
  get_task:
    skip: true
  get_cycles:
    skip: true
  get_relations:
    skip: true
  add_blocker:
    skip: true
  remove_relation:
    skip: true
  add_related:
    skip: true
---

# Linear

Project management integration for engineering teams.

Linear issues map to **tasks** and projects map to **projects** on the AgentOS graph.

## Setup

1. Get your API key from https://linear.app/settings/api
2. Add credential in AgentOS Settings → Connectors → Linear

**Important**: Linear API keys are used WITHOUT the "Bearer" prefix.

## Features

- Full CRUD for tasks (issues)
- Projects and cycles
- Workflow states (customizable per team)
- Sub-tasks via parent_id
- Issue relationships (blocking, related)

## Workflow

Linear uses customizable workflow states per team. Common patterns:

| State Type | Typical Names | Maps to |
|------------|---------------|---------|
| backlog | Backlog, Triage | open |
| unstarted | Todo | open |
| started | In Progress, In Review | in_progress |
| completed | Done | done |
| canceled | Canceled | cancelled |

To change an issue's state, use `update_task` with `state_id` from `get_workflow_states`.

## Priority Scale

| Value | Meaning |
|-------|---------|
| 0 | No priority |
| 1 | Urgent |
| 2 | High |
| 3 | Medium |
| 4 | Low |

## Completing Issues

To mark an issue complete:
1. Call `get_workflow_states` with the issue's team_id
2. Find the state with `type: "completed"`
3. Call `update_task` with the issue id and state_id
