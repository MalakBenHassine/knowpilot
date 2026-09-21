# ADR-0018: Tell the user what no passage names, instead of asking the model not to generalise

- **Status:** Accepted
- **Date:** 2026-09-21

## Context

A manual test asked: "Quelle est la franchise pour un rétroviseur cassé".
The answer was "0 € si réparé / 90 € si remplacé", with a valid citation.

The passage lists a **glass breakage** guarantee (*bris de glace*) and never
mentions mirrors. The model decided on its own that a mirror is glass. The
citation was real, but the claim was not in the document. No citation check
can see this, because every number cited exists.

The same passage does not name the **windscreen** either. For a windscreen,
answering from glass breakage is plainly right. For a mirror, it may be
right, since many contracts do cover mirror glass, but this document does
not say so. The problem is not that the model answers. The problem is that
the answer reads as if the document named the thing.

## What was measured

Every attempt to make the **model** tell "named" from "implied" was measured
on the real contract. Each variant was run 2 to 4 times.

| Attempt | Result |
| --- | --- |
| A prompt rule: "never extend a rule to a case the passages do not name" | Refused 2/2 **with** a trailing "?"; answered 4/4 **without** it |
| A few-shot example about mirrors | Refused 3/3, but only because it copies that example: overfitting to the test |
| A few-shot example from another domain (bicycles and fuel allowances) | Answered 3/3 |
| Reasoning effort "medium" | Refused 1/3 |
| A separate grader, gpt-oss-20b ("is it named?") | Correct without "?", **wrong 2/2 with "?"** |
| A separate grader, qwen3-27b | Wrong on both mirror variants |

The judgement "named or only implied" is what is unstable, whichever model
makes it. A trailing question mark was enough to flip every variant.

## Decision

The work is split between a model and the code, so that each part does what
it can do reliably:

1. **A small model does the easy part.** `gpt-oss-20b`, through LangChain's
   `with_structured_output`, copies out of the question the **name** of the
   thing it asks about: `["rétroviseur"]`, `["code PIN"]`, `["GH-482-KT"]`.
   It returns `[]` for questions about a topic ("what do I pay each month").
   - Measured on 19 questions × 3 runs, with and without "?": **57/57 stable
     and correct**.
   - The call runs **concurrently with retrieval**, so it adds no latency.
   - Groq budgets each model separately, so this call does not spend the
     answering model's daily tokens.
2. **The code checks the model's output.** A subject is kept only if it
   literally appears in the question, after folding accents and case. There
   are at most 5 subjects, of at most 60 characters each. An invented
   subject would otherwise produce a false notice.
3. **PostgreSQL does the part that must not vary.** It checks whether every
   word of the subject appears in the passages, after stemming and accent
   folding. It uses the same `french_unaccent` configuration as the keyword
   search, and the same rule of dropping alphabetic words under 3 letters.
4. **The result is a fact, and it is used twice.**
   - The prompt receives the line `Not named in any passage: "rétroviseur"`.
     Rule 6 then asks the model to answer from a broader category **only
     after saying** that the documents do not name the thing. The model is
     given a fact instead of being asked to make a judgement.
   - The response carries `not_in_documents`, and the interface shows it
     above the answer: *"Your documents never mention 'rétroviseur'. This
     answer relies on what they say about something broader."* The notice
     is attached by the code, whatever the model wrote.

The check **fails open**. If the provider fails, the JSON does not match the
schema, or the database errors, the result is "no subject" and the event is
logged. The check can only add a notice. It can never stop an answer.
`KP_GROQ_CHECK_MODEL=` (empty) disables it.

## Consequences

- The evaluation passes **21/21**. The mirror question is refused without
  "?" and answered **with the notice** with "?". Both outcomes are
  acceptable, and neither is an unqualified generalisation. For the
  windscreen, the model writes "the document does not cite the term
  windscreen" by itself, and the notice appears.
- The PIN case no longer demands a refusal. It demands "a refusal, or an
  answer that carries the notice". That is what can be guaranteed. The
  earlier expectation depended on the mood of the model.
- The notice states a fact about **words**, not a judgement about meaning.
  So it also appears on obvious cases: "pare-brise" gets a notice under
  "bris de glace". This is deliberate. Deciding which inferences are
  obvious is exactly the judgement that proved unstable.
- A synonym the stemmer does not know ("auto" / "véhicule") produces a
  notice too. This is honest, because the word is absent, but it can be
  noisy. We have not yet seen it in the evaluation.
- The rule of dropping words under 3 letters was needed here as well. A test
  on the real database reported "le la les" as missing.

## Revisit when

- Synonym notices become frequent. Add a synonym dictionary to the text
  search configuration. It lives in the database, where both checks already
  read it.
- A model is measured that judges "named or implied" reliably across
  punctuation and phrasing. At that point, a grader could replace the
  notice with a refusal where that is the better product.
