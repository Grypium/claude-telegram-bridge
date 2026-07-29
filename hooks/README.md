# Commitment Check — a Stop hook that catches phantom work

An agent writes *"I'll file those issues"* or *"Fixing and rerunning now"* and ends the turn.
Nothing happens. The next turn starts from **your** message, not from the agent's unfinished
commitments, so the intention silently evaporates.

This failure is uniquely bad because it *reads* like completed work. You believe the task is
done. The agent has, in a sense, also discharged its obligation — saying it felt like doing it.
You find out later, if at all.

This hook blocks the turn until the agent does one of three things.

## The rule

A forward-looking commitment must either:

1. **be executed in the same turn**, or
2. **be recorded as a durable artifact** — an issue, a file, a scheduled task — and cited, or
3. **be stated plainly as `NOT DONE:`**, with a reason.

Anything else blocks, and the agent is asked to pick one.

## Install

```bash
./hooks/install.sh              # install (idempotent)
./hooks/install.sh --dry-run    # show what would change
./hooks/install.sh --uninstall  # remove cleanly
```

Merges into `~/.claude/settings.json` without disturbing existing hooks, backs the file up
first, and runs the hook's self-test *before* wiring anything up. Restart Claude Code after.

Respects `CLAUDE_CONFIG_DIR` if you keep your config somewhere else.

## Verify

```bash
python3 hooks/commitment_check.py --selftest
```

```
  [ok] block  "I'll file those issues once the run finishes."
  [ok] block  "Next I'll rerun the corrected test."
  [ok] block  'Fixing and rerunning now.'
  [ok] pass   'Filed as issue #42, and the run is DONE.'
  [ok] pass   'NOT DONE: blocked on credentials, so I stopped.'
  [ok] pass   'RUNNING — launched as PID 4821, will report next turn.'

  all 12 self-test cases passed
```

## What gets through

Phrasing that shows the work happened, or is honestly flagged as outstanding:

`DONE` · `RUNNING` · `NOT DONE` · `issue #42` · `#42` · `tracked in issue` · `blocked on` ·
`PID 4821` · `TODO`

So an agent reporting *"Filed as issue #42"* or *"RUNNING — PID 4821, will report next turn"*
passes. An agent saying *"I'll look into that"* does not.

## Two layers: regex blocks, a model catches misses

Regex alone has one failure that matters. It is not false alarms — a false alarm costs you one
sentence ("already did that"). It is **misses**: phrasings no pattern anticipated, like
*"once the backfill lands I'm swinging back to the vol test"*. A missed commitment is invisible
by construction, because nothing alerts you that nothing happened.

So the two layers are split by which failure they are good at:

| layer | when | cost | catches |
|---|---|---|---|
| **regex** | every turn | ~35ms | the obvious phrasings — blocks the turn immediately |
| **model** | only when regex is silent | 0ms in-turn (detached) | everything regex missed |

The model call runs **detached**. The Stop hook sits in the critical path of every turn, so it
must never wait on a network call — backgrounding makes a hang impossible by construction
rather than by tuning a timeout.

The verdict therefore arrives *after* the turn ends, delivered through the bridge's notify
endpoint. That is better than blocking: it lands as a new message, creating a turn in which the
missed work actually gets done, instead of a turn spent arguing about whether it was missed.

```
SYNC: 35ms  (turn ends here)
detached audit -> INJECTED after ~20s  [phrasing the regex missed]
control (clean message citing #29) -> no injection
```

Enable it in the config:

```json
{"llm_catch_misses": true, "llm_model": "claude-haiku-4-5-20251001", "llm_timeout_sec": 120}
```

Recursion is blocked by an env guard: the spawned child sets `COMMITMENT_CHECK_CHILD`, and any
hook running with it set exits immediately. Without that, the adjudicator would trigger the very
hook that invoked it.

## Wake-trigger check

Separately, and **mechanically** rather than semantically: if a message claims work is running
and will be reported on, the hook checks whether a background task was actually started this
turn (`Monitor`, `Bash` with `run_in_background`, `Agent`, `CronCreate`).

If not, it blocks — because *"I'll report when it lands"* otherwise depends on the agent
happening to be invoked again, which is not a mechanism. This reuses the hook's core principle:
verify the artifact, don't trust the intention. Disable with `{"wake_trigger_check": false}`.

## Tuning

Optional `~/.claude/hooks/commitment_check.config.json` — no reinstall needed:

```json
{
  "extra_patterns": ["\\bI'?ll ping\\b"],
  "extra_absolve":  ["\\bJIRA-\\d+\\b"],
  "disabled": false
}
```

Use `extra_absolve` to teach it your tracker's identifier format.

## Design notes

- **Cannot loop.** `stop_hook_active` short-circuits, and a per-session marker keyed on a hash
  of the message prevents re-challenging identical text.
- **Fails open.** Every error path exits 0. A broken hook must never wedge your session.
- **Deliberately shallow.** It cannot know whether work actually happened — only that the
  message claims future work and shows no evidence of an artifact. That asymmetry is the whole
  point: being asked *"did you actually do that?"* costs seconds, while discovering a phantom
  task two days later can cost a great deal more.

False positives are the intended trade. If the work really is done, saying so concretely —
command output, file changed, issue number — passes the check and is a better message anyway.

## Origin

Written after an agent twice described work as complete that had never been run, in both cases
caught by the human rather than by any tooling. Adding it to memory or instructions did not
help; the fix had to be mechanical.
