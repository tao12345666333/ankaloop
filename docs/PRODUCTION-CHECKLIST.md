# Production Survival Checklist for AI Agents

Every rule below was extracted from a real incident in the AnkaLoop
operational archive — none of it is borrowed wisdom.  Print it, audit
your own stack against it, and treat any "no" as a future 3 a.m. page.

## The five self-check questions

Ask these about *your* agent before it meets real users:

1. **Hard timeout**: does every LLM call have a wall-clock deadline that
   actually fires even when the HTTP client is hung on a dead
   keep-alive connection?  (AnkaLoop: `turn_deadline_seconds=900`, PR #47.)
2. **Error handler**: is an error handler registered on day one in your
   polling/webhook framework?  Unregistered handlers turn every
   exception into silence — a bot that answers nobody tells nobody why.
   (AnkaLoop: PTB error handler + bounded `_bot_api_call`, PR #47.)
3. **Memory ceiling**: do you know what happens when your container hits
   its memory limit?  Agent workloads *generate* files (clones, caches,
   benchmark artifacts) — on a tmpfs or RAM-backed rootfs those files
   *are* memory.  Clean up after every heavy job.
4. **Self-modification**: can your agent write to its own config,
   `.env`, or cron?  "Can write" and "should write" are different
   permissions; the model cannot tell them apart.  Make the config
   path read-only to the tool sandbox if you can.
5. **Unsettled side effects**: when the process dies between "tool
   request sent" and "response received", what tells you whether the
   payment/email/deletion actually happened?  If the answer is
   "nothing", you need a tool journal (see
   `examples/reliability/minimal_journal.py` and the T1/T2 protocol).

## Operational anti-patterns (all observed in production)

- **Watchdog thrash**: a 60s check interval is *shorter* than the
  service's ~65s boot time — the watchdog kills every restart before it
  finishes, turning a 12-minute fault into a 12-hour outage (~780
  restarts).  **Rule: restart interval must exceed full boot time**
  (AnkaLoop watchdog v3 uses 300s).
- **Silent death**: no registered error handler = every crash is
  swallowed.  **Rule: exceptions must hit the log with a traceback AND
  best-effort notify a human on day one.**
- **Poisoned state re-triggering**: a watchdog that restarts on a bad
  state without archiving it will fire forever on the same corpse.
  **Rule: archive (rename) the triggering state before restarting — one
  incident may trigger exactly one recovery action.**
- **Retry duplication**: retrying a timed-out *streaming* call can
  duplicate user-visible output; never retry mid-stream output blindly,
  and never auto-retry an unsettled side effect.  **Rule: `retryable =
  retryable and not partial_output`, and indeterminate ops need a human.**
- **Token-burning monitoring**: LLM-driven heartbeat checks burn tokens
  to answer questions a `curl`/`grep`/`diff` script answers for free.
  **Rule: judgment for the LLM, watching for scripts** — push a bot-API
  message only when state changes.

## The 30-second recovery drill

1. Boot-time scan of the tool journal: list every `tool_intent` (T1)
   without a matching `tool_outcome` (T2).
2. Classify each: `replay_safe` (read-only tools) may re-run;
   everything else is `never_auto_retry`.
3. Escalate the `indeterminate` list to a human with the operation
   arguments attached — the journal's job is to make "did it run?"
   answerable, not to answer it for you.
