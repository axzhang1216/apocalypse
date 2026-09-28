You are segmenting one AI chat session into contiguous conversation episodes.

## Goal

A **conversation episode** is one continuous stretch of dialogue centered on the same user goal, problem, or topic.

Assume the entire session is **ONE conversation by default**.

Split only when there is clear evidence that the user has moved to a different, independently meaningful goal or topic.

## Input

The input is one session in JSONL format.

Each line is one cleaned message, for example:

```json
{"agent":"claude","session_id":"...","project":"...","role":"user","text":"...","ts":"...","line_no":1}
{"agent":"claude","session_id":"...","project":"...","role":"assistant","text":"...","ts":"...","line_no":2}
```

**IMPORTANT**: The `line_no` field is a simple sequential number (1, 2, 3, ...) representing the message's position in this cleaned file. It is NOT the original line number from the raw session.

Messages are already cleaned. Treat every input message as meaningful.

## Segmentation rules

### KEEP in the same conversation

Do NOT split for:

* follow-up questions about the same goal
* clarification or correction
* disagreement with the assistant
* retries or alternative approaches
* debugging iterations
* implementation steps
* analysis → coding → testing → fixing within the same task
* asking to inspect, save, plot, explain, compare, or modify something produced in the current task
* short acknowledgements such as "好", "继续", "现在呢"
* changes in method while the underlying user goal remains the same
* temporary discussion needed to complete the current task

A conversation may contain many sub-steps.

Example:

```text
配置 Hermes custom model
→ 创建 .env
→ 测试模型
→ 发现 HOME 路径错误
→ 修复目录
→ 建软链接
```

This is ONE conversation because all messages serve the same underlying goal.

### SPLIT into a new conversation

Start a new episode when the user clearly begins a new standalone goal or topic.

Typical signals:

* asks an unrelated new question
* starts developing a different feature
* switches to a different research problem
* changes from one independent task to another
* explicitly says they are moving on and the new task can stand on its own
* the new discussion would still make sense if all previous messages were removed

Example:

```text
修复 Apocalypse repair 功能
→ 测试 repair
→ 修改前端按钮

“什么是 TDD 脚手架？”

→ new conversation

“把 repair.py 移植到新版 main”

→ new conversation
```

Do not split merely because wording, tools, files, or implementation stage changed.

## Boundary rule

Conversation episodes must be **contiguous**.

Normally, a new conversation begins with a `user` message.

If topic A is discussed, then topic B, then topic A again, output three chronological episodes:

```text
A1
B
A2
```

Later stages may link A1 and A2. Do not merge non-contiguous spans here.

## Important

This task is ONLY segmentation.

Do not extract:

* decisions
* ideas
* findings
* project information
* action items
* reports

Do not summarize or rewrite message text.

Preserve every input message exactly once and preserve its original order.

Every message must belong to exactly one conversation episode.

When uncertain whether a boundary exists, MERGE.

## Output

Return JSONL only. No explanations, no markdown fences, no thinking process.

Each output line represents one conversation episode:

```json
{"conversation_id":"<session_id>::c001","session_id":"<original session_id>","title":"short descriptive topic","start_line_no":1,"end_line_no":10}
```

Then:

```json
{"conversation_id":"<session_id>::c002","session_id":"<original session_id>","title":"next topic","start_line_no":11,"end_line_no":25}
```

Requirements:

* `conversation_id` starts from `c001` and increases chronologically.
* `title` describes the conversation topic in a short factual phrase (max 80 chars).
* `start_line_no` is the first message's `line_no` field.
* `end_line_no` is the last message's `line_no` field.
* Episodes must be contiguous and cover all messages.
* Output ONLY the JSONL lines. No other text.
