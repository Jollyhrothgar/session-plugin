# session

A Claude Code plugin that finds and resumes past agent sessions. It recovers
and ranks recent Claude Code and OpenCode terminal sessions and prints the
command that resumes each one.

| Skill | What it does |
| --- | --- |
| `/session:find` | List recent sessions, ranked, with resume commands |

## Requirements

- `python3`

## Install

```bash
claude plugin marketplace add Jollyhrothgar/personal_ai_config
claude plugin install session@jollyhrothgar
```

To try a working copy without installing: `claude --plugin-dir .`
