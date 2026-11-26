"""Follow-up rewriting and conversation memory."""

from __future__ import annotations

from .core import *
from .config import CONVERSATION_MAX_TURNS

CONVERSATION_REWRITE_PROMPT = """Decide whether the latest user question depends on the conversation, then rewrite it as a standalone question.

<conversation>
{conversation}
</conversation>

<latest_question>
{question}
</latest_question>

Rules:
- If the latest question is a follow-up that refers to the conversation, rewrite it as a standalone question.
- Replace "he", "she", "him", "her", "his", or "hers" with the person named in the conversation.
- Replace "it" or "its" with the thing or topic being discussed.
- Replace "they" or "them" with the people or things being discussed.
- Replace "this", "that", "these", or "those" with the specific thing or things they point to.
- If the latest question is a short or elliptical follow-up (for example "why?", "and his wife?", "what about that?", or "what are the opening hours?"), turn it into a complete question about the relevant topic in the conversation.
- A question is a follow-up only when it refers to the conversation or asks for another detail about the same subject. For example, after a conversation about Nijo Market, "what are the opening hours?" and "is there parking?" are follow-ups, but "how do I cook pasta?" is not.
- If the latest question is already a complete standalone question, or it introduces a different subject or topic that is not part of the conversation (a new place, person, product, or event), output exactly UNRELATED.
- Never add a conversation entity to a question that did not refer to the conversation or ask about the same subject. For example, do not turn "what is the weather in Tokyo?" into a question about the conversation's subject.
- Keep the user's intent, wording, language, and constraints.
- Use only names and topics stated in the conversation. Do not answer the question and do not add outside facts.
Output only the standalone question on one line, or the single word UNRELATED, with no quotes, labels, or explanation.

Standalone question:"""

CONVERSATION_SUMMARY_PROMPT = """Maintain a compact running summary of an ongoing chat for future follow-up questions.

<existing_summary>
{summary}
</existing_summary>

<new_turns>
{turns}
</new_turns>

Rules:
- Merge the new turns into the existing summary. Do not repeat the same fact twice.
- Preserve names, places, dates, entities, user preferences and constraints, decisions, conclusions, open questions, and references (IDs, URLs, section names) needed to look details up again.
- Drop greetings, acknowledgements, repeated facts, raw tool output, duplicate retrieved chunks, and abandoned speculation.
- Do not add facts that are not stated in the existing summary or the new turns.
- Use short bullet points and at most 250 words.
- Output only the updated summary, with no heading, label, or explanation.

Updated summary:"""

CONVERSATION_UNRELATED = "unrelated"

# A short question is ambiguous: it may be an elliptical follow-up even when it
# contains no pronoun. Content words are counted without the words that almost
# every question uses. Longer questions carry enough topic words to be treated
# as standalone, which avoids a model call on every turn.
FOLLOW_UP_MAX_CONTENT_WORDS = 6

CONVERSATION_STOPWORDS = frozenset(
    "a an the and or but if then than that this these those there here "
    "what which who whom whose when where why how is are was were be been "
    "being do does did done have has had can could should would will may "
    "might must shall to of in on at for from with about into over after "
    "before under between during any some all more most other another "
    "such only just also very much many few own same so not no nor too "
    "i me my we us you your he him his she her it its they them their "
    "theirs please tell give show list find get".split()
)

class ConversationResolution(NamedTuple):
    """Result of interpreting one question against the recent conversation."""

    question: str
    is_follow_up: bool = False
    is_unrelated: bool = False

def conversation_content_terms(text: str) -> set[str]:
    """Return the topic-bearing terms of a question or answer."""
    return {
        token
        for token in chat.tokenize(text)
        if len(token) > 1 and token not in CONVERSATION_STOPWORDS
    }

def conversation_overlap_ratio(question: str, turns: list[dict]) -> float:
    """Return the strongest topic overlap between a question and recent questions.

    The ratio is measured against the smaller of the two questions, so a single
    generic shared word (for example "market" in an unrelated question) does
    not dominate a longer question.
    """
    if not turns:
        return 0.0
    question_terms = conversation_content_terms(question)
    if not question_terms:
        return 1.0
    best = 0.0
    for turn in turns:
        asked = str(
            turn.get("resolved_question") or turn.get("question") or ""
        )
        asked_terms = conversation_content_terms(asked)
        if not asked_terms:
            continue
        shared = question_terms & asked_terms
        if not shared:
            continue
        best = max(
            best, len(shared) / min(len(question_terms), len(asked_terms))
        )
    return best

def question_relates_to_conversation(
    question: str, turns: list[dict], summary: str = ""
) -> bool:
    """Return True when a question shares a substantial topic with memory.

    The comparison deliberately uses the previous *questions* and the curated
    running summary, not the previous answers. Answers are long, so almost any
    new question shares some word with them (for example a Nijo Market answer
    mentions "Japan", which would make a later visa question look related). A
    question starts a fresh context unless it shares more than half of the
    smaller side of the comparison with one of the last asked questions, which
    keeps genuine same-topic questions in the same context while a different
    subject refreshes it. A summary match is deliberately stricter: it needs at
    least two content terms and half of the question's terms, so a generic word
    in a compacted summary cannot keep a stale topic alive.
    """
    if conversation_overlap_ratio(question, turns) > 0.5:
        return True
    summary = " ".join(str(summary or "").split())
    if not summary:
        return False
    question_terms = conversation_content_terms(question)
    summary_terms = conversation_content_terms(summary)
    if not question_terms or not summary_terms:
        return False
    shared = question_terms & summary_terms
    required = 1 if len(question_terms) == 1 else max(
        2, math.ceil(len(question_terms) * 0.5)
    )
    return len(shared) >= required

def is_unrelated_response(response: str) -> bool:
    """Return True when the conversation model flags a new topic."""
    text = chat.THINK_PATTERN.sub("", response).strip().strip('"\'`""')
    normalized = re.sub(r"[^a-z]+", " ", text.lower()).strip()
    return bool(normalized) and normalized.split()[0] == CONVERSATION_UNRELATED

def normalize_question_for_comparison(text: str) -> str:
    """Fold wording that does not change a question's meaning.

    Case, punctuation, and articles are ignored so a model that only re-adds
    "a" or "the" (for example "what is git worktree?" -> "What is a Git
    worktree?") is not mistaken for a rewritten follow-up. That mistake kept
    a stale conversation alive and sent the new topic to the previous topic's
    context.
    """
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    return " ".join(token for token in tokens if token not in {"a", "an", "the"})

def question_needs_context(question: str, turns: list[dict]) -> bool:
    """Return True when ``question`` may refer to an earlier turn."""
    if not turns:
        return False
    text = question.strip()
    if not text:
        return False
    if FOLLOW_UP_PRONOUNS.search(text) or FOLLOW_UP_OPENERS.search(text):
        return True
    # Very short or elliptical questions such as "why?", "how so?", or
    # "what are the opening hours?" usually depend on the previous turn even
    # when they contain no explicit pronoun. Longer questions have enough
    # topic words to stand on their own and skip the model call.
    return len(conversation_content_terms(text)) <= FOLLOW_UP_MAX_CONTENT_WORDS

def format_conversation(
    turns: list[dict],
    summary: str = "",
    limit: int | None = CONVERSATION_MAX_TURNS,
) -> str:
    """Format the running summary and recent turns for a conversation prompt."""
    lines: list[str] = []
    summary = "\n".join(
        " ".join(line.split())
        for line in str(summary or "").splitlines()
        if line.strip()
    )
    if summary:
        lines.append("Earlier conversation summary:")
        lines.append(summary)
        lines.append("")
    selected = list(turns) if limit is None else turns[-limit:]
    for turn in selected:
        asked = " ".join(str(turn.get("question", "")).split())
        resolved = " ".join(str(turn.get("resolved_question", "")).split())
        if resolved and resolved != asked:
            asked = f"{asked} (resolved: {resolved})"
        answer = " ".join(str(turn.get("answer", "")).split())
        if len(answer) > CONVERSATION_ANSWER_CHARS:
            answer = answer[:CONVERSATION_ANSWER_CHARS].rstrip() + "..."
        lines.append(f"User: {asked}")
        lines.append(f"Assistant: {answer}")
    return "\n".join(lines)

def conversation_answer_prefix(summary: str) -> str:
    """Render the running summary for the answer prompt.

    The summary is not retrieved evidence. It is included only so the writer
    can interpret references, respect user constraints, and maintain continuity
    after older turns have been compacted. The prompt template still requires
    factual claims to come from the current context chunks.
    """
    summary = "\n".join(
        " ".join(line.split())
        for line in str(summary or "").splitlines()
        if line.strip()
    )
    if not summary:
        return ""
    return (
        "<conversation_memory>\n"
        "Earlier conversation memory. Use it only to interpret the user's "
        "question, resolve references, and respect user constraints. It is "
        "not retrieved evidence; factual claims must still come from the "
        "context chunks.\n"
        f"{summary}\n"
        "</conversation_memory>\n\n"
    )

def clean_conversation_summary(text: str) -> str:
    """Normalize one model-written running summary."""
    text = chat.THINK_PATTERN.sub("", str(text or "")).strip()
    text = re.sub(r"^```(?:markdown)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()
    text = re.sub(
        r"^(?:updated\s+)?summary\s*[::]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    return "\n".join(lines).strip()

def clamp_conversation_summary(text: str) -> str:
    """Keep the running summary below the configured character budget."""
    text = text.strip()
    if len(text) <= CONVERSATION_SUMMARY_MAX_CHARS:
        return text
    shortened = text[:CONVERSATION_SUMMARY_MAX_CHARS].rsplit(" ", 1)[0].rstrip()
    return shortened + " ..."

def fallback_conversation_summary(evicted: list[dict], existing: str) -> str:
    """Deterministic summary used when the helper model cannot summarize."""
    parts: list[str] = []
    if existing.strip():
        parts.append(existing.strip())
    questions: list[str] = []
    for turn in evicted:
        question = " ".join(str(turn.get("question") or "").split())
        if question and question not in questions:
            questions.append(question)
    if questions:
        parts.append("Earlier questions: " + "; ".join(questions))
    return clamp_conversation_summary("\n".join(parts))

def clean_standalone_question(response: str) -> str | None:
    """Extract one clean standalone question from a model response."""
    text = chat.THINK_PATTERN.sub("", response).strip()
    text = re.sub(
        r"^(?:standalone|rewritten|search|new)\s+question\s*[::]\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^(?:question|q)\s*[::]\s*", "", text, flags=re.IGNORECASE)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None
    text = " ".join(lines[0].split()).strip('"\'`""')
    if not text or text.lower() in {"none", "n/a", "unchanged"}:
        return None
    if len(text) > 600:
        return None
    return text

def rewrite_follow_up(
    question: str,
    turns: list[dict],
    summary: str = "",
) -> ConversationResolution | None:
    """Resolve references in a follow-up or detect a completely new topic."""
    if not turns and not summary.strip():
        return None
    prompt = CONVERSATION_REWRITE_PROMPT.format(
        conversation=format_conversation(turns, summary),
        question=question,
    )
    unchanged: str | None = None
    for model in dict.fromkeys((CONVERSATION_MODEL, chat.EXPANSION_MODEL)):
        try:
            response = chat.generate_text(
                prompt,
                model=model,
                temperature=0.0,
                num_predict=256,
                num_ctx=chat.ANSWER_NUM_CTX,
                stop=["\n\n", "</latest_question>"],
            )
        except RuntimeError:
            continue
        if is_unrelated_response(response):
            # A model may call a standalone question on the same subject a new
            # topic. Keep the conversation when the question still shares its
            # subject with an earlier question; there is nothing to rewrite,
            # but a later follow-up must still be able to refer back to it.
            if question_relates_to_conversation(question, turns, summary):
                return ConversationResolution(question, is_follow_up=False)
            return ConversationResolution(
                question, is_follow_up=False, is_unrelated=True
            )
        cleaned = clean_standalone_question(response)
        if not cleaned:
            continue
        if (
            normalize_question_for_comparison(cleaned)
            != normalize_question_for_comparison(question)
        ):
            return ConversationResolution(cleaned, is_follow_up=True)
        unchanged = cleaned
    if unchanged is not None:
        # The model returned the question unchanged. It is a follow-up only
        # when it still shares a topic with the recent questions; otherwise it
        # is a fresh question and the stale context must be dropped.
        related = question_relates_to_conversation(question, turns, summary)
        return ConversationResolution(
            question, is_follow_up=related, is_unrelated=not related
        )
    # No conversation model was available. Fall back to a lexical decision so
    # an unrelated question still gets a fresh context.
    related = question_relates_to_conversation(question, turns, summary)
    return ConversationResolution(
        question, is_follow_up=related, is_unrelated=not related
    )


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .config import CONVERSATION_ANSWER_CHARS, CONVERSATION_MODEL, CONVERSATION_SUMMARY_MAX_CHARS
from .programming_docs import FOLLOW_UP_OPENERS, FOLLOW_UP_PRONOUNS
