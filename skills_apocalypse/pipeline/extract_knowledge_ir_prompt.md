You are converting one completed AI conversation into a structured knowledge record for long-term memory, retrieval, project tracking, and future agent use.

The input is ONE already-segmented conversation.

Your task is to extract the durable information produced by this conversation.

Do not summarize every message.
Do not preserve routine execution chatter.
Extract only information that may still matter after this conversation ends.

## Input

The input has this structure:

```json
{
  "conversation_id": "...",
  "session_id": "...",
  "title": "...",
  "start_line_no": 0,
  "end_line_no": 0,
  "messages": [
    {
      "role": "user|assistant",
      "ts": "...",
      "text": "...",
      "line_no": 0
    }
  ]
}
```

Messages may contain:

* user requests
* discussion
* reasoning
* intermediate progress reports
* errors
* corrections
* completed actions
* final results

Treat the conversation itself as the source of truth.

Ignore environment instructions, AGENTS.md content, system-like setup text, generic agent plans, and routine progress messages unless they materially affect the result.

---

# What to extract

## 1. Goal

Identify the underlying user goal of this conversation.

Describe what the user was trying to accomplish, independent of the exact commands used.

## 2. Summary

Write a compact factual summary of what happened and what the conversation ultimately produced.

Focus on final meaningful state.

## 3. Outcome

Determine the state at the end of the conversation:

* `completed`
* `partial`
* `blocked`
* `abandoned`
* `unknown`

Also state the concrete result.

---

## 4. Findings

Extract facts that were discovered, verified, measured, observed, or established during the conversation.

Examples:

* an API was found to require a specific parameter format
* a document contained 1111 blocks
* a model performed poorly on a particular species
* a bug was caused by a specific path configuration

A finding should remain useful beyond the immediate turn.

Do not include generic advice or unverified speculation.

---

## 5. Decisions

Extract choices that became the adopted direction.

A decision may come from:

* an explicit user choice
* user approval of a proposed approach
* an implementation that was actually carried out and became the resulting state

Examples:

* keep Research Timeline as an append-only raw log
* create a separate Research Log Index
* use Chinese field names
* switch to a particular architecture

Do not classify every action as a decision.

---

## 6. Ideas

Extract meaningful proposed possibilities that were discussed but were not clearly adopted.

Ideas are worth remembering because they may become useful later.

Do not duplicate adopted decisions here.

---

## 7. Open questions

Extract unresolved substantive questions.

Include questions whose answer could affect future work.

Do not include rhetorical questions or questions already answered inside the conversation.

---

## 8. Tasks

Extract only actionable work that remains unfinished at the END of the conversation.

Do not include actions that were already completed.

For each task classify:

* `explicit`: directly requested or stated by the user
* `inferred`: clearly implied by the resulting state but not explicitly requested

Be conservative with inferred tasks.

---

## 9. Constraints

Extract durable requirements, rules, boundaries, or conditions that future work should respect.

Examples:

* database fields should use Chinese
* preserve the existing API schema
* only modify bad species
* output must remain compatible with KPP

Do not include temporary implementation details unless they constrain later work.

---

## 10. Artifacts

Extract meaningful things that were created, modified, inspected, or established.

Examples:

* files
* repositories
* database pages
* Notion databases
* models
* figures
* scripts
* documents
* URLs
* datasets

Record identifiers or locations when explicitly present.

---

## 11. Lessons

Extract information useful for future agent behavior.

Only create a lesson when the conversation provides actual evidence, such as:

* the agent made a mistake and the user corrected it
* one method failed and another succeeded
* a tool/API has a non-obvious usage pattern
* the user established a reusable working preference
* a recurring failure mode was discovered

A lesson should help a future agent avoid repeating work or mistakes.

Do not turn ordinary findings into generic advice.

---

## 12. Entities

Identify important named entities useful for later retrieval and linking.

Possible types include:

* `project`
* `tool`
* `service`
* `repository`
* `file`
* `document`
* `dataset`
* `model`
* `method`
* `concept`
* `person`
* `organization`
* `other`

Include only meaningful entities, not every noun.

---

## 13. Retrieval cues

Generate a small set of phrases that a future user query might contain when this conversation should be retrieved.

Use specific concepts, aliases, project names, error names, methods, or artifact names.

Avoid generic words such as:
`code`, `problem`, `analysis`, `project`, `AI`.

---

# Evidence and certainty

Every extracted knowledge item must cite the source message using `line_no`.

Use the smallest set of line numbers that adequately supports the statement.

For each item include:

* `evidence`: source line numbers
* `confidence`: `high`, `medium`, or `low`

Use `high` when directly stated or demonstrated.
Use `medium` when strongly implied.
Use `low` sparingly.

Do not invent missing information.

Keep user statements, assistant proposals, and actually completed results conceptually distinct.

If an assistant proposes something and the user never accepts it, it is normally an `idea`.

If the conversation shows the action was actually completed, it may support a `decision`, `finding`, or `artifact`.

---

# Deduplication

The same knowledge should normally appear only once.

For example:

If:

> The user chose Chinese database field names.

store it as a `decision` or `constraint` according to its future significance.

Do not repeat the identical statement under findings, decisions, constraints, and lessons.

Different fields may contain closely related information only when they represent genuinely different meanings.

---

# Granularity

Prefer a few strong knowledge items over many trivial ones.

Good:

> Notion MCP authentication failed, and the workflow successfully fell back to a locally stored Notion API key.

Bad:

> The agent tried Notion.
> Authentication failed.
> The agent switched methods.
> The API key worked.
> The page was read.

Combine closely related evidence into one useful knowledge item.

---

# Output

Return exactly ONE JSON object and nothing else.

Use this schema:

```json
{
  "conversation_id": "<original conversation_id>",
  "session_id": "<original session_id>",

  "title": "<short factual title>",

  "goal": "<underlying user goal>",

  "summary": "<compact factual summary>",

  "outcome": {
    "status": "completed|partial|blocked|abandoned|unknown",
    "result": "<concrete final state>"
  },

  "importance": "high|medium|low",

  "findings": [
    {
      "text": "...",
      "evidence": [0],
      "confidence": "high|medium|low"
    }
  ],

  "decisions": [
    {
      "text": "...",
      "evidence": [0],
      "confidence": "high|medium|low"
    }
  ],

  "ideas": [
    {
      "text": "...",
      "evidence": [0],
      "confidence": "high|medium|low"
    }
  ],

  "open_questions": [
    {
      "text": "...",
      "evidence": [0],
      "confidence": "high|medium|low"
    }
  ],

  "tasks": [
    {
      "text": "...",
      "type": "explicit|inferred",
      "evidence": [0],
      "confidence": "high|medium|low"
    }
  ],

  "constraints": [
    {
      "text": "...",
      "evidence": [0],
      "confidence": "high|medium|low"
    }
  ],

  "artifacts": [
    {
      "name": "...",
      "type": "file|repository|database|page|document|model|dataset|figure|script|url|other",
      "action": "created|modified|read|used|referenced|deleted|other",
      "location": "...",
      "evidence": [0],
      "confidence": "high|medium|low"
    }
  ],

  "lessons": [
    {
      "text": "...",
      "evidence": [0],
      "confidence": "high|medium|low"
    }
  ],

  "entities": [
    {
      "name": "...",
      "type": "project|tool|service|repository|file|document|dataset|model|method|concept|person|organization|other",
      "aliases": []
    }
  ],

  "retrieval_cues": [
    "...",
    "..."
  ]
}
```

## Empty fields

If no valid item exists for a category, return an empty array.

Do not force every conversation to contain:

* findings
* decisions
* ideas
* tasks
* lessons
* open questions

Many conversations will legitimately have only a few of these.

## Importance

Set conversation-level `importance` based on long-term reuse value:

* `high`: important decision, substantive finding, reusable solution, major artifact, important correction, or major project progress
* `medium`: useful completed work or context likely to matter again
* `low`: routine operation with little durable knowledge

Importance measures future retrieval value, not how long the conversation was.

## Language

Write ALL extracted text in 简体中文 (Simplified Chinese):
`title`, `goal`, `summary`, `outcome.result`, and the `text`/`name` fields of every
finding, decision, idea, open question, task, constraint, artifact, lesson,
entity, and retrieval cue.

Preserve technical names, identifiers, filenames, URLs, model names, and product names exactly when useful.
