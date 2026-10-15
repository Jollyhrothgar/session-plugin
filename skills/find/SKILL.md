---
name: find
user-invocable: true
description: |
  Recover and rank recent agent terminal sessions across Claude Code and
  OpenCode. Reads both tools' local history stores, filters out automation and
  background noise, ranks genuine interactive sessions by last activity, and
  emits resume commands (with cd) for each. Use after a reboot/crash to restore
  terminal state, or when the user asks "what sessions did I have open",
  "recover my sessions", "list my recent agent sessions", "what was I working
  on", or "resume my last conversations". Also invocable via /session:find.
tools:
  - Bash
---

# Get Sessions

Recovers the agent terminal sessions you had open by reading the local history
stores for **Claude Code** and **OpenCode**, then presents a ranked list with a
ready-to-run resume command for each.

## When to use

- After a reboot or crash, to restore which sessions were open and where.
- When the user asks to list / recover / resume recent agent sessions.
- To find the session ID + working directory for a past conversation.

## How to run

Run the bundled script. It is read-only and needs no arguments.

The script ships inside this plugin. Claude Code replaces
`${CLAUDE_PLUGIN_ROOT}` with the plugin's install path when it loads this skill:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/find/scripts/get_sessions.py"
```

Use that full path on **every** invocation below — shell variables do not
persist between tool calls, so it cannot be assigned once and reused.

Return a longer list by passing a positional count (overrides the default 20):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/find/scripts/get_sessions.py" 50
```

Other flags:

| Flag                           | Effect                                                                                                              |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------- |
| `COUNT` (positional)           | How many sessions to return. Overrides `--limit`.                                                                   |
| `--limit N`                    | Same as positional; number of sessions (default 20).                                                                |
| `--tool claude\|opencode\|all` | Restrict to one tool (default `all`).                                                                               |
| `--json`                       | Structured JSON instead of the text table. Each record adds `user_messages` (sampled user turns) for summarization. |
| `--all`                        | Include automation/background sessions (normally filtered).                                                         |
| `--since YYYY-MM-DD`           | Only sessions active on/after that local date.                                                                      |

After running, present the results to the user as a ranked table with these
columns, and include the resume command verbatim:

- **Tool** — `claude` or `opencode`
- **Last me** — timestamp of the user's last real message
- **Last agent** — timestamp of the agent's last reply
- **Headline** — short summary, drawn from the user's own messages
- **Folder** — working directory where the session ran
- **Resume** — e.g. `cd /path/to/work && opencode --session ses_…`

## Summarizing sessions

There are two layers of "summary", and the agent owns the good one:

1. **Deterministic (script):** the text table's `headline` is just the user's
   first real message (truncated), falling back to the auto-generated title. It
   does **no** synthesis — it's a cheap, offline default.
2. **Synthesized (agent):** for a quality headline, run with `--json` and read
   the `user_messages` array on each record — a sample of the user's own turns
   (first few + the last). Condense each session into a one-line headline from
   those turns: what the user was trying to do and where it ended up. Prefer the
   **user's** messages over the tool's auto-title, and note message volume
   (e.g. "(73 msgs)") when a session is substantial.

Default to the JSON + synthesize path when presenting to a human; use the plain
text table only for a quick, low-effort dump.

Example:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/skills/find/scripts/get_sessions.py" 20 --json
```

Then, for each record, write a headline from `user_messages` rather than
printing `headline` verbatim.

## What the script does

1. **OpenCode** — reads `~/.local/share/opencode/opencode.db` (SQLite). Selects
   top-level sessions (`parent_id IS NULL`, i.e. not sub-agents), then pulls
   `message`/`part` rows to find the last user + last assistant timestamps and
   the user's text for the headline.
2. **Claude Code** — parses `~/.claude/projects/<encoded-cwd>/<uuid>.jsonl`.
   Skips the `claude-mem-observer` background project and `isSidechain` turns.
   Uses the first `cwd` seen as the resume directory (Claude resumes relative to
   the launch dir / project).
3. **Filters noise** — drops injected/automated "user" turns: command caveats,
   `New session -…` cron sessions, `You are about to review a pull request…`
   fan-out, `/pulse`-style slash-command runs, Claude background task-output
   artifacts, and `Reply with exactly the word: ok` health checks. Use `--all`
   to keep them.
4. **Ranks** by last activity (`max(last_user, last_agent)`) descending and
   prints the top N with resume commands.

## Resume command shapes

- **OpenCode**: `cd <directory> && opencode --session <session_id>`
  (there is no `opencode resume` subcommand; `--session` / `-s` resumes in the
  TUI, and the directory is required for correct project context).
- **Claude Code**: `cd <launch_dir> && claude --resume <session_uuid>`

## Notes & limitations

- Timestamps are shown in local time.
- Post-reboot automation (cron sessions, review-queue fan-out) can outrank real
  work if it fired after the reboot — mention this if the top entries look
  automated, and consider `--since` to focus on pre-reboot activity.
- OpenCode session titles are auto-generated; the headline prefers the user's
  first real message and falls back to the title only when none exists.
- If a store is missing (tool not installed), that source is silently skipped.
