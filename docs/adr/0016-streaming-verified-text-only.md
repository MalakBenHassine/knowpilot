# ADR-0016: Stream answers, but only text the guards have already accepted

- **Status:** Accepted
- **Date:** 2026-09-21

## Context

Streaming - words appearing as they are generated - makes an assistant feel
fast. It collides with this product's promise: an answer is shown only if it
cites a passage it was given. Guard 3 checks citations on the **complete**
answer; a naive stream would display text before anyone knows whether it will
be rejected.

Measured before deciding: answers take 0.5 to 1.9 seconds end to end, so the
gain is real on long answers and negligible on short ones.

## Options considered

| Option | Pros | Cons |
| --- | --- | --- |
| Do not stream | Simplest; nothing unverified can be shown | Long answers wait for their last word |
| Stream everything, retract on rejection | Fastest first word | The user reads unsourced text, then watches it vanish - the product visibly contradicting itself |
| **Hold back until the first valid citation, then stream** | Nothing shown that the verdict can reject; long answers still stream | A one-sentence answer cited at its end arrives in one piece |

## Decision

**Hold back until the first valid citation.** Guard 3 grounds an answer as
soon as it contains ONE citation to a passage that was sent. Before that
point, the text may still be refused, so it is kept on the server. From that
point, the verdict can no longer be "unsourced": the held text is released
and the rest streams token by token. The refusal word is never sent; if it
appears after a citation, generation is stopped and the final event withdraws
the answer.

- `POST /api/chat/stream`, Server-Sent Events: `stage`, `token`, `done`,
  `error`. `done` is the authoritative answer and replaces what was shown.
- Every status code (401, 422, 429, 503) is settled **before** the stream
  opens; only a mid-stream provider failure becomes an `error` event.
- One verdict function (`_verdict`) serves both routes, so the streamed and
  the blocking answer cannot disagree.
- `GuardedChain` is a `Runnable` subclass implementing `ainvoke` AND `astream`:
  the previous `RunnableLambda` could not stream - it would have silently
  turned `astream` into a blocking call.
- Streams are closed with `aclosing`: `break` inside `async for` does not
  close an async generator, and the provider stream would have kept
  generating - and billing - until garbage collection.
- The client reads the stream with `fetch`, because `EventSource` cannot send
  a POST body or the CSRF header. `X-Accel-Buffering: no` stops a buffering
  proxy from turning the stream back into a wait.
- The "Generating..." stage is now reported by the server instead of being
  guessed with a 1.2 s timer.

## Consequences

- First visible text after 0.82 s on a detailed real question answered in
  1.89 s; 506 fragments; no reasoning leaked into the answer.
- **The first real streamed question found a production bug unrelated to
  streaming:** gpt-oss reasons inside its output budget, and at the default
  effort a detailed question spent all 700 tokens thinking and answered
  nothing (a 503). Fixed with `reasoning_effort="low"` - reasoning 7x shorter,
  measured - and `max_tokens=1000`; evaluation still 14/14, and a detailed
  question was added to it.
- A client that disconnects is not refunded (the tokens were spent), and its
  disconnection stops the generation.

## Revisit when

- Guard 3 becomes stricter than "at least one valid citation" (for example,
  every sentence must be cited). The release point must then move to the end
  of each verified sentence, or streaming must stop.
- Answers routinely become long enough that the held first sentence feels
  slow.
