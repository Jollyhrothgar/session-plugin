#!/usr/bin/env python3
"""Recover recent agent terminal sessions from Claude Code and OpenCode histories.

Reads local session stores for both tools, filters out automation/background
noise, ranks the remaining interactive sessions by last activity, and prints a
ranked list with resume commands.

Stores read (read-only):
  - OpenCode: ~/.local/share/opencode/opencode.db  (SQLite; session/message/part)
  - Claude Code: ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl

Usage:
  python3 get_sessions.py [COUNT] [--limit N] [--tool claude|opencode|all]
                          [--json] [--all] [--since YYYY-MM-DD]

Flags:
  COUNT         Positional: how many sessions to return. Overrides --limit.
                e.g. `get_sessions.py 50` returns the 50 most recent.
  --limit N     Number of sessions to show (default 20).
  --tool T      Restrict to one tool (default all).
  --json        Emit structured JSON instead of the text table. Each record
                includes `user_messages` (a sample of the user's own turns) so
                an agent can synthesize a richer headline than the deterministic
                first-line default.
  --all         Include automation/background sessions (normally filtered).
  --since DATE  Only sessions with last activity on/after DATE (local time).
"""

import argparse
import datetime
import glob
import json
import os
import re
import sqlite3
import sys

OPENCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def local_dt(ms):
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).astimezone()


def fmt(ms):
    d = local_dt(ms)
    return d.strftime("%Y-%m-%d %H:%M") if d else "        -       "


def clean(text):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)  # strip xml-ish wrappers
    text = re.sub(r"\s+", " ", text).strip()
    return text


# Prefixes/patterns that indicate injected, automated, or non-conversational
# "user" turns rather than something the human actually typed.
_NOISE_PREFIXES = (
    "caveat:",
    "the messages below",
    "this session is being continued",
    "this session changed files",
    "a coding session is starting",
    "you are about to review a pull request",
    "automated pr review",
    "automated pr-review",
    "<command",
    "local-command",
)


def is_real_user_text(text):
    """True if the text looks like a genuine human-typed message."""
    if not text:
        return False
    ct = clean(text)
    if not ct:
        return False
    low = ct.lower()
    if low.startswith(_NOISE_PREFIXES):
        return False
    # Claude background-task output artifacts leak in as user-role turns.
    if re.search(r"toolu_\w+\s+/private/tmp/claude-501", ct):
        return False
    if re.search(r"\.output (completed|running|failed)", ct):
        return False
    if ct.startswith("[Request interrupted"):
        return False
    # Slash-command automation (pulse/tool-releases health runs, etc.).
    if re.match(r'["\']?/(personal-pulse|pulse-summary|tool-releases)', low):
        return False
    if low in ("(no content)", "ok", "reply with exactly the word: ok"):
        return False
    return True


def is_automation_title(title):
    t = title or ""
    if t.startswith("New session -"):
        return True
    if t == "Simple confirmation response":
        return True
    return False


# --------------------------------------------------------------------------- #
# OpenCode
# --------------------------------------------------------------------------- #
def load_opencode(scan_limit=250):
    if not os.path.exists(OPENCODE_DB):
        return []
    out = []
    con = sqlite3.connect(f"file:{OPENCODE_DB}?mode=ro", uri=True)
    cur = con.cursor()
    cur.execute(
        "SELECT id, directory, title, time_created, time_updated "
        "FROM session WHERE parent_id IS NULL "
        "ORDER BY time_updated DESC LIMIT ?",
        (scan_limit,),
    )
    sessions = cur.fetchall()
    for sid, directory, title, tc, tu in sessions:
        cur.execute(
            "SELECT id, data, time_created FROM message "
            "WHERE session_id=? ORDER BY time_created",
            (sid,),
        )
        last_user = last_asst = None
        user_ids = []
        for mid, data, mtc in cur.fetchall():
            try:
                d = json.loads(data)
            except Exception:
                continue
            role = d.get("role")
            t = (d.get("time", {}) or {}).get("created") or mtc
            if role == "user":
                last_user = t
                user_ids.append(mid)
            elif role == "assistant":
                last_asst = t
        user_texts = []
        if user_ids:
            q = "SELECT message_id, data FROM part WHERE message_id IN (%s)" % ",".join(
                "?" * len(user_ids)
            )
            cur.execute(q, user_ids)
            for _mid, pdata in cur.fetchall():
                try:
                    p = json.loads(pdata)
                except Exception:
                    continue
                if p.get("type") == "text" and p.get("text"):
                    ct = clean(p["text"])
                    if is_real_user_text(ct):
                        user_texts.append(ct)
        out.append(
            {
                "tool": "opencode",
                "id": sid,
                "dir": directory,
                "resume_dir": directory,
                "title": title,
                "last_user": last_user,
                "last_asst": last_asst,
                "user_texts": user_texts,
                "automation": is_automation_title(title),
            }
        )
    con.close()
    return out


# --------------------------------------------------------------------------- #
# Claude Code
# --------------------------------------------------------------------------- #
def load_claude():
    if not os.path.isdir(CLAUDE_PROJECTS):
        return []
    out = []
    for pdir in glob.glob(CLAUDE_PROJECTS + "/*"):
        # Background "observer" sessions are automation, not terminals.
        if "claude-mem-observer" in os.path.basename(pdir):
            continue
        for jf in glob.glob(pdir + "/*.jsonl"):
            first_cwd = last_cwd = None
            last_user = last_asst = None
            title = None
            user_texts = []
            try:
                for line in open(jf):
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("type") == "summary" and not title:
                        title = o.get("summary")
                    if o.get("cwd"):
                        if first_cwd is None:
                            first_cwd = o["cwd"]
                        last_cwd = o["cwd"]
                    if o.get("isSidechain"):
                        continue
                    ts = o.get("timestamp")
                    tsms = None
                    if ts:
                        try:
                            tsms = (
                                datetime.datetime.fromisoformat(
                                    ts.replace("Z", "+00:00")
                                ).timestamp()
                                * 1000
                            )
                        except Exception:
                            pass
                    typ = o.get("type")
                    msg = o.get("message", {}) if isinstance(o.get("message"), dict) else {}
                    if typ == "user":
                        if o.get("isMeta"):
                            continue
                        c = msg.get("content")
                        txt = None
                        if isinstance(c, str):
                            txt = c
                        elif isinstance(c, list):
                            txt = " ".join(
                                b.get("text", "")
                                for b in c
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        if txt and is_real_user_text(txt):
                            last_user = tsms
                            user_texts.append(clean(txt))
                    elif typ == "assistant":
                        last_asst = tsms
            except Exception:
                continue
            if not user_texts:
                continue  # no genuine human turns -> not an interactive terminal
            out.append(
                {
                    "tool": "claude",
                    "id": os.path.basename(jf)[:-6],
                    "dir": last_cwd or first_cwd,
                    # Claude resumes relative to the launch (first) cwd / project.
                    "resume_dir": first_cwd or last_cwd,
                    "title": title,
                    "last_user": last_user,
                    "last_asst": last_asst,
                    "user_texts": user_texts,
                    "automation": False,
                }
            )
    return out


# --------------------------------------------------------------------------- #
# Ranking + output
# --------------------------------------------------------------------------- #
def last_activity(r):
    return max([x for x in (r["last_user"], r["last_asst"]) if x] or [0])


def resume_cmd(r):
    d = r["resume_dir"] or "."
    if r["tool"] == "claude":
        return f'cd {d} && claude --resume {r["id"]}'
    return f'cd {d} && opencode --session {r["id"]}'


def headline(r):
    if r["user_texts"]:
        first = r["user_texts"][0]
        return (first[:150] + "…") if len(first) > 150 else first
    return r["title"] or "(no summary)"


def sample_user_messages(r, head=5, cap=280):
    """Representative user turns for the agent to synthesize a headline from.

    Returns the first `head` messages plus the last one (to capture where the
    conversation ended up), de-duplicated and order-preserved, each capped to
    `cap` chars. This is the raw material the invoking agent should summarize,
    rather than relying on the deterministic first-line `headline`.
    """
    texts = r["user_texts"]
    if not texts:
        return []
    picked = list(texts[:head])
    if texts[-1] not in picked:
        picked.append(texts[-1])
    seen, out = set(), []
    for t in picked:
        t = (t[:cap] + "…") if len(t) > cap else t
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def main():
    ap = argparse.ArgumentParser(description="Recover recent agent terminal sessions.")
    ap.add_argument(
        "count",
        nargs="?",
        type=int,
        default=None,
        help="how many sessions to return (positional; overrides --limit; default 20)",
    )
    ap.add_argument("--limit", type=int, default=20, help="how many sessions to return (default 20)")
    ap.add_argument("--tool", choices=["claude", "opencode", "all"], default="all")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="include automation/background sessions")
    ap.add_argument("--since", help="only sessions active on/after this date (YYYY-MM-DD, local)")
    args = ap.parse_args()

    # Positional count wins when supplied, so `get_sessions.py 50` returns 50.
    limit = args.count if args.count is not None else args.limit

    rows = []
    if args.tool in ("all", "opencode"):
        rows += load_opencode()
    if args.tool in ("all", "claude"):
        rows += load_claude()

    if not args.all:
        rows = [r for r in rows if not r["automation"] and last_activity(r) > 0]

    if args.since:
        try:
            cutoff = datetime.datetime.strptime(args.since, "%Y-%m-%d")
            cutoff = cutoff.replace(tzinfo=datetime.datetime.now().astimezone().tzinfo)
            cutoff_ms = cutoff.timestamp() * 1000
            rows = [r for r in rows if last_activity(r) >= cutoff_ms]
        except ValueError:
            print(f"bad --since date: {args.since}", file=sys.stderr)
            return 2

    rows.sort(key=last_activity, reverse=True)
    rows = rows[:limit]

    if args.json:
        out = []
        for r in rows:
            out.append(
                {
                    "tool": r["tool"],
                    "id": r["id"],
                    "dir": r["dir"],
                    "title": r["title"],
                    "last_user": fmt(r["last_user"]).strip() if r["last_user"] else None,
                    "last_agent": fmt(r["last_asst"]).strip() if r["last_asst"] else None,
                    "n_user_msgs": len(r["user_texts"]),
                    "headline": headline(r),
                    "user_messages": sample_user_messages(r),
                    "resume": resume_cmd(r),
                }
            )
        print(json.dumps(out, indent=2))
        return 0

    if not rows:
        print("No interactive sessions found.")
        return 0

    for i, r in enumerate(rows, 1):
        print(f"\n#{i}  [{r['tool']}]  last_me={fmt(r['last_user'])}  last_agent={fmt(r['last_asst'])}")
        print(f"    dir     : {r['dir']}")
        print(f"    headline: {headline(r)}")
        print(f"    resume  : {resume_cmd(r)}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
