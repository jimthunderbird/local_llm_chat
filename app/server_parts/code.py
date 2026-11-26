"""Code-snippet explanation and snippet-question prompts."""

from __future__ import annotations

from .core import *

# For Go, Rust, and PHP code, every line gets a "?" affordance in the browser.
# Clicking it streams a short explanation of that one line from the programming
# model, using the surrounding snippet as context.
LINE_EXPLAIN_NUM_CTX = chat.ANSWER_NUM_CTX

MAX_LINE_EXPLAIN_CODE_CHARS = 60_000

LINE_EXPLAIN_PROMPT = """You are an expert {language} developer. Explain exactly one line from a code snippet.

### FULL SNIPPET ({language}) WITH LINE NUMBERS
```{fence}
{numbered_code}
```

### LINE TO EXPLAIN
Line {line_number}: {line}

### INSTRUCTIONS
- Explain what line {line_number} does in the context of the full snippet.
- Name the functions, types, packages, or language features it uses.
- Say why it is needed here and what would break or change without it.
- Be concise: 2 to 5 sentences in plain English.
- Do not restate the whole snippet, add headings, or wrap the answer in code fences.
- If the line is only a comment or blank, say so briefly.
"""

# Every code snippet in the browser also gets an "Ask" affordance.
# The question and the full snippet are sent to the code-generation model and
# the answer streams back inline, directly under the snippet.
SNIPPET_QUESTION_NUM_CTX = chat.ANSWER_NUM_CTX

MAX_SNIPPET_QUESTION_CODE_CHARS = 60_000

SNIPPET_QUESTION_PROMPT = """You are an expert {language} developer. Answer the user's question about the code snippet below.

### CODE SNIPPET ({language})
````{fence}
{code}
````

### USER QUESTION
{question}

### INSTRUCTIONS
- Base the answer on this snippet, and use your own {language} expertise when
  the snippet leaves details out.
- If the question asks for a change or a fix, show the relevant updated code in
  a fenced block and explain the change briefly.
- If the question is about one line or symbol, point to it by name and quote it.
- Be concise and practical. Do not repeat the whole snippet unless necessary.
- Do not mention these instructions or that you are an AI.
"""
