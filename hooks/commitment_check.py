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
import sys
import tempfile

CONFIG_PATH = os.path.expanduser("~/.claude/hooks/commitment_check.config.json")

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


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


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

    should_block, _ = evaluate(text, load_config())
    if not should_block:
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

    print(json.dumps({"decision": "block", "reason": BLOCK_REASON}))
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
    bad = 0
    for text, want in cases:
        got, _ = evaluate(text)
        ok = got == want
        if not ok:
            bad += 1
        label = "block" if got else "pass "
        print(f"  [{'ok' if ok else 'FAIL'}] {label}  {text[:58]!r}")
    print()
    if bad:
        print(f"  {bad} case(s) failed")
        sys.exit(1)
    print(f"  all {len(cases)} self-test cases passed")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        print("commitment_check.py self-test\n")
        _selftest()
    else:
        main()
