#!/usr/bin/env python3
"""Stop hook: block turn-end when the agent states an intention without acting on it.

THE FAILURE MODE
    The agent writes "I'll file those issues" or "Fixing and rerunning now" and then ends the
    turn. Nothing happens. The next turn begins from your message, not from the agent's
    unfinished commitments, so the intention evaporates silently.

    This is uniquely bad because it *reads* like completed work. You believe a task is done.
    The agent has, in a sense, also discharged its obligation -- saying it felt like doing it.
    You find out later, if at all.

THE RULE ENFORCED
    A forward-looking commitment must either
      (a) be executed in the same turn,
      (b) be recorded as a durable artifact (issue, file, scheduled task), or
      (c) be stated plainly as NOT DONE, with a reason.

    Anything else blocks the turn and the agent is asked to pick one.

DESIGN NOTES
  * Fires at most once per turn. `stop_hook_active` short-circuits, and a per-session marker
    keyed on a hash of the message prevents re-challenging identical text. It cannot loop.
  * Fails OPEN. Every error path exits 0. A broken hook must never wedge your session.
  * Text-matching is deliberately shallow. It cannot know whether work happened -- it only
    knows the message *claims* future work and shows no evidence of an artifact. Being asked
    "did you actually do that?" is cheap; discovering a phantom task two days later is not.

CONFIG (optional)  ~/.claude/hooks/commitment_check.config.json
    {
      "extra_patterns": ["\\\\bI'?ll ping\\\\b"],
      "extra_absolve":  ["\\\\bJIRA-\\\\d+\\\\b"],
      "disabled": false
    }

SELF-TEST
    python3 commitment_check.py --selftest
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

CONFIG_PATH = os.path.expanduser("~/.claude/hooks/commitment_check.config.json")

# Set when we spawn a child model call, so the child's own Stop hook exits immediately.
# Without this the adjudicator would trigger the very hook that invoked it, forever.
CHILD_ENV = "COMMITMENT_CHECK_CHILD"

# Tools that constitute "I actually started background work"
BG_TOOLS = {"Monitor", "CronCreate", "ScheduleWakeup", "Task", "Agent"}

# Phrasing that claims work is in flight and will be reported later
RUNNING_CLAIMS = [
    r"\bRUNNING\b", r"\bin the background\b", r"\bwhen it (lands|finishes|completes)\b",
    r"\bI'?ll report\b", r"\bwill report\b", r"\breport (back )?(when|once)\b",
    r"\bkicked off\b", r"\blaunched\b",
]

# Future-tense phrasing that reads as completed work but produces no artifact.
PATTERNS = [
    r"\bI'?ll\s+(file|run|test|build|add|create|fix|check|write|implement|deploy|update|rerun|re-run|start|kick off|look into|investigate)\b",
    r"\bI\s+will\s+(file|run|test|build|add|create|fix|check|write|implement|deploy|update)\b",
    r"\b(next|then)\s+I'?ll\b",
    r"\bLet me\s+\w+\s+(next|after)\b",
    r"\b(Fixing|Running|Rerunning|Re-running|Building|Filing|Testing|Deploying|Starting)\s+(and|it|that|this|now|next)\b",
    r"\bgoing to\s+(file|run|test|build|create|fix|rerun|deploy)\b",
    r"\bI'?ll\s+(get|circle)\s+back\b",
]

# Evidence the commitment was actually discharged, or is honestly flagged as outstanding.
ABSOLVE = [
    r"\bNOT DONE\b",
    r"\bnot yet (run|done|executed|filed)\b",
    r"\bTODO\b",
    r"\bfiled as (issue|#)\b",
    r"\btracked (as|in) (issue|#)\b",
    r"\bissue #\d+\b",
    r"\b#\d+\b",
    r"\bDONE\b",
    r"\bRUNNING\b",
    r"\bblocked on\b",
    r"\bPID \d+\b",
]

BLOCK_REASON = (
    "COMMITMENT CHECK — your message states an intention to do something.\n"
    "Before ending the turn, choose one:\n"
    "  1. DO IT NOW (preferred) — run the command / create the issue / make the edit.\n"
    "  2. RECORD IT — file an issue or write it to a tracked file, then cite the identifier.\n"
    "  3. SAY IT PLAINLY — write 'NOT DONE:' and why, so the user is not told an intention "
    "as if it were an action.\n\n"
    "This fired because stating a plan in prose creates no artifact and does not survive the "
    "turn boundary. If the work is genuinely done, say so concretely (command output, file "
    "changed, issue number) and this will pass."
)


WAKE_REASON = (
    "WAKE TRIGGER CHECK — your message says work is running and that you will report on it, "
    "but no background task was started this turn.\n"
    "Either start one now (Monitor, or Bash with run_in_background, so you are notified when "
    "it finishes), or say plainly that the user must ask again for the result.\n\n"
    "Otherwise 'I'll report when it lands' depends on you happening to be invoked again, which "
    "is not a mechanism."
)


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def turn_tool_uses(transcript_path):
    """Tool names used since the last user message -- i.e. during THIS turn.

    Mechanical, not semantic: we are asking "was background work actually started", the same
    artifact-over-intention question the whole hook exists to enforce.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return set()
    turn, names = [], set()
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("type")
                if t == "user":
                    # Tool RESULTS also arrive as type "user". Resetting on those wiped the
                    # turn's tool calls, so the check only ever saw the final message and
                    # fired even when a Monitor had just been started. Reset only on a real
                    # user message -- one carrying actual text rather than a tool_result.
                    c = (d.get("message") or {}).get("content")
                    is_tool_result = isinstance(c, list) and any(
                        isinstance(x, dict) and x.get("type") == "tool_result" for x in c)
                    if not is_tool_result:
                        turn = []
                elif t == "assistant":
                    turn.append(d)
        for d in turn:
            for c in (d.get("message") or {}).get("content") or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    names.add(c.get("name") or "")
                    inp = c.get("input") or {}
                    if c.get("name") == "Bash" and inp.get("run_in_background"):
                        names.add("Bash:background")
    except Exception:
        return set()
    return names


# Citing a concrete handle for the running work IS the artifact -- the same standard the rest
# of the hook applies. A task id, PID, job number or service name is checkable; "it's running"
# is not.
HANDLE_CITED = re.compile(
    r"\b(task|monitor|job|watcher|PID|pid)\s+[`\"']?[A-Za-z0-9_.-]{3,}"
    r"|\b[a-z0-9]{8,12}\b(?=[^\w]*(still|armed|running|polling|active|watching))"
    r"|\bnssm\b|\bservice\s+[A-Za-z][A-Za-z0-9_-]{2,}",
    re.IGNORECASE)


def wants_wake_trigger(text, tools):
    """True if the message claims background work but nothing tracks it.

    Satisfied by EITHER starting a background task this turn OR citing a handle for one that
    already exists. The turn-scoped check alone was wrong: a persistent Monitor started in an
    earlier turn is invisible to it, so every later status update would be nagged even though
    the mechanism was in place. (It fired on exactly that case in real use.)
    """
    if not any(re.search(p, text, re.IGNORECASE) for p in RUNNING_CLAIMS):
        return False
    if (BG_TOOLS & tools) or "Bash:background" in tools:
        return False
    return not HANDLE_CITED.search(text)


# Cheap gate before spending a model call: skip messages with no forward-looking marker at
# all. Deliberately LOOSE -- it is tuned for recall, and anything it lets through is decided
# by the model, not by this list.
FUTURE_HINT = re.compile(
    r"\b(i'?ll|i will|we'?ll|we will|next|then|after|once|going to|plan|planning|"
    r"should|need to|want to|plan to|plans? to|plans?:|plan is|plan for|"
    r"plan on|later|soon|upcoming|plan out|queue|queued|pending|todo|follow[- ]?up)\b",
    re.IGNORECASE)


def looks_worth_checking(text):
    """Skip trivially-clean messages so most turns still cost nothing."""
    if not text or len(text.strip()) < 40:
        return False
    return bool(FUTURE_HINT.search(text))


def llm_adjudicate(text, cfg):
    """Ask a small model whether this is a real unfulfilled commitment.

    Returns True (block), False (allow), or None (undecided -> keep the regex verdict).

    Called when the strict regex did NOT fire. That is deliberate and is the whole point:
    the dangerous failure is a MISS, not a false alarm. A false alarm costs one sentence
    ("already did that"); a miss costs a phantom task discovered days later. So the model is
    spent on recall, where regex is weak, and never on turns regex already caught.

    Fails to None on ANY error: a hook that can hang is worse than a hook that misses.
    """
    if os.environ.get(CHILD_ENV):
        return None
    model = cfg.get("llm_model", "claude-haiku-4-5-20251001")
    # Generous by design: this runs DETACHED, so a slow call costs nothing but its own
    # process. The old 8s was sized for a synchronous hook and silently timed out on real
    # messages -- the CLI alone takes ~6.5s on a trivial prompt.
    timeout = float(cfg.get("llm_timeout_sec", 120))
    prompt = (
        "You audit an AI agent's final message to its user for UNFULFILLED COMMITMENTS.\n\n"
        "Block ONLY if the message promises future work that produced no artifact this turn.\n"
        "Do NOT block if the work was done, is cited (issue/PR/commit/file/PID/task id), is "
        "explicitly flagged NOT DONE or blocked, or is genuinely running in the background.\n"
        "Describing results, findings or reasoning is never a commitment.\n\n"
        "Reply with ONLY compact JSON: {\"block\": true|false, \"why\": \"<=12 words\"}\n\n"
        "MESSAGE:\n" + text[-6000:]
    )
    env = dict(os.environ)
    env[CHILD_ENV] = "1"
    try:
        p = subprocess.run(
            ["claude", "-p", prompt, "--model", model],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        out = (p.stdout or "").strip()
        i, j = out.find("{"), out.rfind("}")
        if i < 0 or j <= i:
            return None
        d = json.loads(out[i:j + 1])
        v = d.get("block")
        return bool(v) if isinstance(v, bool) else None
    except Exception:
        return None


def spawn_audit(text, cfg):
    """Fire the model check into the background and return immediately.

    The synchronous path must never wait on a network call: the Stop hook sits in the critical
    path of every turn, so a slow or hung model would stall the session for everyone. Detaching
    makes that impossible by construction rather than by timeout tuning.

    The consequence is that the verdict arrives AFTER the turn ends -- which turns out to be
    better than blocking. It lands as a new message, creating a turn in which the missed work
    actually gets done, instead of a turn spent arguing about whether it was missed.
    """
    if os.environ.get(CHILD_ENV):
        return
    try:
        fd, path = tempfile.mkstemp(prefix="commitment_", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        env = dict(os.environ)
        env[CHILD_ENV] = "1"
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--audit", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True, env=env,
        )
    except Exception:
        pass          # a failed audit must never affect the turn


def notify(message, cfg):
    """Inject the finding back into the session via the bridge's notify endpoint."""
    url = cfg.get("notify_url") or os.environ.get("COMMITMENT_NOTIFY_URL") \
        or f"http://127.0.0.1:{os.environ.get('NOTIFY_PORT', '9998')}/notify"
    body = json.dumps({"message": message}).encode()
    try:
        import urllib.request
        req = urllib.request.Request(url, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10).read()
        return True
    except Exception:
        return False


def run_audit(path):
    """Child process: adjudicate, and notify only if a commitment was actually missed."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except Exception:
        return
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass
    cfg = load_config()
    # strip the child guard so the model call itself is permitted
    os.environ.pop(CHILD_ENV, None)
    verdict = llm_adjudicate(text, cfg)
    os.environ[CHILD_ENV] = "1"
    if verdict is not True:
        return
    notify(
        "COMMITMENT CHECK (async) — the last message appears to promise work that was not "
        "done and left no artifact.\n\n"
        "If that is right: do it now, or record it as an issue/task and cite the identifier, "
        "or state plainly that it is NOT DONE.\n"
        "If it is wrong, say so in one line and carry on.",
        cfg,
    )


def marker_path(session_id):
    tag = hashlib.md5((session_id or "default").encode()).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f".claude_commitment_{tag}")


def last_assistant_text(transcript_path):
    """Final assistant text block in the transcript. Empty string on any problem."""
    if not transcript_path or not os.path.exists(transcript_path):
        return ""
    last = ""
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                content = (d.get("message") or {}).get("content") or []
                parts = [c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text"]
                if parts:
                    last = "\n".join(parts)
    except Exception:
        return ""
    return last


def evaluate(text, cfg=None):
    """Return (should_block, matched_patterns). Pure -- this is what the self-test drives."""
    cfg = cfg or {}
    if cfg.get("disabled"):
        return False, []
    if not text or not text.strip():
        return False, []
    pats = PATTERNS + list(cfg.get("extra_patterns") or [])
    absolve = ABSOLVE + list(cfg.get("extra_absolve") or [])
    hits = [p for p in pats if re.search(p, text, re.IGNORECASE)]
    if not hits:
        return False, []
    if any(re.search(a, text, re.IGNORECASE) for a in absolve):
        return False, hits
    return True, hits


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    # Never loop: if this hook already blocked once this turn, let the turn end.
    if payload.get("stop_hook_active"):
        sys.exit(0)

    text = last_assistant_text(payload.get("transcript_path"))
    if not text:
        sys.exit(0)

    cfg = load_config()
    should_block, _ = evaluate(text, cfg)

    tools = turn_tool_uses(payload.get("transcript_path"))
    wake = cfg.get("wake_trigger_check", True) and wants_wake_trigger(text, tools)

    # Regex fired -> block. No model call: a false alarm here is cheap to answer, and paying
    # latency to second-guess a hit we would mostly keep anyway buys nothing.
    #
    # Regex silent -> ask the model. This is where the expensive failure lives. Strict patterns
    # cannot anticipate every phrasing ("once that lands I'll swing back to the vol test"), and
    # a missed commitment is invisible by construction -- nobody is alerted that nothing
    # happened. Recall is worth the seconds; precision is not.
    if not should_block and cfg.get("llm_catch_misses", True) and looks_worth_checking(text):
        spawn_audit(text, cfg)          # detached; cannot delay or hang this turn

    if not should_block and not wake:
        sys.exit(0)

    # Don't re-challenge the exact same message twice.
    mark = marker_path(payload.get("session_id"))
    sig = hashlib.md5(text[-4000:].encode("utf-8", "replace")).hexdigest()
    try:
        if os.path.exists(mark) and open(mark).read().strip() == sig:
            sys.exit(0)
        with open(mark, "w") as f:
            f.write(sig)
    except Exception:
        pass

    reason = BLOCK_REASON if should_block else ""
    if wake:
        reason = (reason + "\n\n" if reason else "") + WAKE_REASON
    print(json.dumps({"decision": "block", "reason": reason}))
    sys.exit(0)


def _selftest():
    cases = [
        ("I'll file those issues once the run finishes.", True),
        ("Next I'll rerun the corrected test.", True),
        ("Fixing and rerunning now.", True),
        ("I'm going to deploy the fix.", True),
        ("I'll get back to you on that.", True),
        ("Filed as issue #42, and the run is DONE.", False),
        ("NOT DONE: blocked on credentials, so I stopped.", False),
        ("I'll file that — tracked in issue #17.", False),
        ("The tests pass and I committed the change.", False),
        ("Here are the results: mean +2.3%, t=1.9.", False),
        ("", False),
        ("RUNNING — launched as PID 4821, will report next turn.", False),
    ]
    wake_cases = [
        ("RUNNING — backfill going, I'll report when it lands.", set(), True),
        ("Backfill monitor beuvw391t still armed.", set(), False),
        ("RUNNING — trades backfill; watcher task b7un5ca6w is polling.", set(), False),
        ("RUNNING — launched, PID 4821 will report.", set(), False),
        ("RUNNING — scan launched.", {"Monitor"}, False),
        ("Kicked off the 30-day backfill.", {"Bash"}, True),
        ("Results: mean +2.3%, t=1.9.", set(), False),
    ]
    bad = 0
    for text, want in cases:
        got, _ = evaluate(text)
        ok = got == want
        if not ok:
            bad += 1
        label = "block" if got else "pass "
        print(f"  [{'ok' if ok else 'FAIL'}] {label}  {text[:58]!r}")
    for text, tools, want in wake_cases:
        got = wants_wake_trigger(text, tools)
        ok = got == want
        if not ok:
            bad += 1
        print(f"  [{'ok' if ok else 'FAIL'}] wake={'block' if got else 'pass '}  {text[:52]!r}")
    print()
    if bad:
        print(f"  {bad} case(s) failed")
        sys.exit(1)
    print(f"  all {len(cases) + len(wake_cases)} self-test cases passed")


if __name__ == "__main__":
    if "--audit" in sys.argv:
        run_audit(sys.argv[sys.argv.index("--audit") + 1])
    elif "--selftest" in sys.argv:
        print("commitment_check.py self-test\n")
        _selftest()
    else:
        main()
