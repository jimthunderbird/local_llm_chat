"""Programming-language and coding-question detection."""

from __future__ import annotations

from .core import *

def normalize_language(language: str | None) -> str:
    """Return the canonical display name for a programming language."""
    raw = (language or "").strip()
    aliases = {
        "go": GO_LANGUAGE,
        "golang": GO_LANGUAGE,
        "rust": RUST_LANGUAGE,
        "php": PHP_LANGUAGE,
    }
    return aliases.get(raw.lower(), raw)

def language_uses_official_docs(language: str | None) -> bool:
    """Return True when code answers must come from official docs."""
    return normalize_language(language) in DOCUMENTATION_LANGUAGES

def language_chat_model(language: str | None) -> str:
    """Return the answer-generation model for ``language``."""
    return (
        PROGRAMMING_MODEL
        if language_uses_official_docs(language)
        else chat.ANSWER_MODEL
    )

def language_coding_model(language: str | None) -> str:
    """Return the code-generation model for ``language``."""
    return (
        PROGRAMMING_MODEL
        if language_uses_official_docs(language)
        else CODING_MODE_MODEL
    )

def language_code_fence(language: str | None) -> str:
    """Return the Markdown fence language for ``language`` code samples."""
    return LANGUAGE_CODE_FENCES.get(normalize_language(language), "")

def language_coding_directive(language: str | None) -> str:
    """Build the language requirement for generated code."""
    language = normalize_language(language)
    if not language_uses_official_docs(language):
        return ""
    fence = language_code_fence(language) or language.lower()
    return f"""### LANGUAGE REQUIREMENT (active language: {language})
The user is working in {language}. Every code sample, snippet, command, and
API reference in the answer MUST be written in {language}.
- Use complete, idiomatic {language} and fence every code block as
  ```{fence}.
- If the question names another programming language or does not name any
  language, still answer in {language}; never emit code in another language.
- Use only the standard library and documented {language} APIs unless the
  retrieved context explicitly names a dependency.

"""

def coding_prompt_prefix(language: str | None) -> str:
    """Return the coding-mode prompt for the detected language."""
    return CODING_MODE_PROMPT_PREFIX + language_coding_directive(language)

def github_code_fallback_allowed(language: str | None) -> bool:
    """Return False when GitHub code search must not be used."""
    return not language_uses_official_docs(language)

# GitHub's search API takes a language name ("Python"), not the spelling used
# in a question ("python3"). Frameworks map to the language they are written
# in, and the extensions are used to pick source files in repository fallback.
GITHUB_LANGUAGE_MAP: dict[str, tuple[str, tuple[str, ...]]] = {
    "python": ("Python", (".py",)),
    "python3": ("Python", (".py",)),
    "javascript": ("JavaScript", (".js", ".jsx", ".mjs", ".cjs")),
    "typescript": ("TypeScript", (".ts", ".tsx")),
    "node": ("JavaScript", (".js", ".mjs", ".cjs")),
    "node.js": ("JavaScript", (".js", ".mjs", ".cjs")),
    "deno": ("TypeScript", (".ts", ".js")),
    "java": ("Java", (".java",)),
    "c++": ("C++", (".cpp", ".cc", ".cxx", ".hpp", ".h")),
    "cpp": ("C++", (".cpp", ".cc", ".cxx", ".hpp", ".h")),
    "c#": ("C#", (".cs",)),
    "csharp": ("C#", (".cs",)),
    ".net": ("C#", (".cs",)),
    "dotnet": ("C#", (".cs",)),
    "golang": ("Go", (".go",)),
    "rust": ("Rust", (".rs",)),
    "ruby": ("Ruby", (".rb",)),
    "php": ("PHP", (".php",)),
    "swift": ("Swift", (".swift",)),
    "kotlin": ("Kotlin", (".kt", ".kts")),
    "scala": ("Scala", (".scala",)),
    "haskell": ("Haskell", (".hs",)),
    "elixir": ("Elixir", (".ex", ".exs")),
    "erlang": ("Erlang", (".erl",)),
    "clojure": ("Clojure", (".clj",)),
    "lua": ("Lua", (".lua",)),
    "perl": ("Perl", (".pl", ".pm")),
    "objective-c": ("Objective-C", (".m", ".mm")),
    "dart": ("Dart", (".dart",)),
    "flutter": ("Dart", (".dart",)),
    "julia": ("Julia", (".jl",)),
    "fortran": ("Fortran", (".f90", ".f95", ".f")),
    "cobol": ("COBOL", (".cob", ".cbl")),
    "assembly": ("Assembly", (".asm", ".s")),
    "asm": ("Assembly", (".asm", ".s")),
    "sql": ("SQL", (".sql",)),
    "mysql": ("SQL", (".sql",)),
    "postgres": ("SQL", (".sql",)),
    "postgresql": ("SQL", (".sql",)),
    "sqlite": ("SQL", (".sql",)),
    "bash": ("Shell", (".sh", ".bash")),
    "shell": ("Shell", (".sh", ".bash")),
    "zsh": ("Shell", (".sh", ".zsh")),
    "powershell": ("PowerShell", (".ps1",)),
    "html": ("HTML", (".html", ".htm")),
    "css": ("CSS", (".css",)),
    "sass": ("CSS", (".scss", ".sass")),
    "less": ("CSS", (".less",)),
    "react": ("JavaScript", (".js", ".jsx", ".ts", ".tsx")),
    "reactjs": ("JavaScript", (".js", ".jsx", ".ts", ".tsx")),
    "vue": ("JavaScript", (".vue", ".js", ".ts")),
    "vuejs": ("JavaScript", (".vue", ".js", ".ts")),
    "angular": ("TypeScript", (".ts",)),
    "svelte": ("JavaScript", (".svelte", ".js", ".ts")),
    "next.js": ("JavaScript", (".js", ".jsx", ".ts", ".tsx")),
    "nuxt": ("JavaScript", (".js", ".ts", ".vue")),
    "django": ("Python", (".py",)),
    "flask": ("Python", (".py",)),
    "fastapi": ("Python", (".py",)),
    "rails": ("Ruby", (".rb",)),
    "laravel": ("PHP", (".php",)),
    "spring": ("Java", (".java",)),
    "express": ("JavaScript", (".js", ".ts")),
    "pandas": ("Python", (".py",)),
    "numpy": ("Python", (".py",)),
    "pytorch": ("Python", (".py",)),
    "tensorflow": ("Python", (".py",)),
    "jax": ("Python", (".py",)),
    "sklearn": ("Python", (".py",)),
    "scikit-learn": ("Python", (".py",)),
    "tailwind": ("CSS", (".css", ".html")),
    "tailwindcss": ("CSS", (".css", ".html")),
    "webpack": ("JavaScript", (".js",)),
    "vite": ("JavaScript", (".js", ".ts")),
    "babel": ("JavaScript", (".js",)),
    "jest": ("JavaScript", (".js", ".ts")),
    "pytest": ("Python", (".py",)),
    "android": ("Kotlin", (".kt", ".java")),
    "ios": ("Swift", (".swift",)),
    "unity": ("C#", (".cs",)),
    "unreal": ("C++", (".cpp", ".h")),
    "opengl": ("C++", (".cpp", ".h")),
    "vulkan": ("C++", (".cpp", ".h")),
    "webgl": ("JavaScript", (".js",)),
    "wasm": ("WebAssembly", (".wat", ".wasm")),
}

GITHUB_DEFAULT_EXTENSIONS = tuple(
    sorted(
        {
            extension
            for _language, extensions in GITHUB_LANGUAGE_MAP.values()
            for extension in extensions
        }
    )
)

# Filler and code-request words add no signal to a GitHub query. Technical
# nouns such as "class", "function", and "data structure" are deliberately
# kept because they are often the most useful search terms.
GITHUB_QUERY_STOPWORDS = frozenset(chat.FUZZY_ANCHOR_STOPWORDS) | frozenset(
    "i you he she it we they me him her us them my your his its our their "
    "code sample samples snippet snippets example examples demo demonstrate "
    "write show give provide generate create implement build debug refactor "
    "fix explain teach walkthrough walkthrough want need want like "
    "using use used implementation program script".split()
)

# The main language pattern uses \b, which cannot match names that end in a
# symbol ("C++" and "C#"), nor a leading dot (".NET"). This supplementary
# pattern keeps those common languages usable for GitHub queries.
GITHUB_EXTRA_LANGUAGE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:c\+\+|c#|\.net)(?![A-Za-z0-9_])",
    re.IGNORECASE,
)

# Words that name a programming language, runtime, or framework. A language
# mention alone is not enough: the question must also ask for code/design
# help or name a programming structure.
PROGRAMMING_LANGUAGE_PATTERN = re.compile(
    r"\b(?:"
    r"python|python3|javascript|typescript|node(?:\.js)?|deno|"
    r"java|c\+\+|cpp|c#|csharp|\.net|dotnet|golang|rust|ruby|"
    r"php|swift|kotlin|scala|haskell|elixir|erlang|clojure|lua|perl|"
    r"objective-c|dart|flutter|julia|fortran|cobol|assembly|asm|"
    r"sql|mysql|postgres(?:ql)?|sqlite|mongodb|redis|"
    r"bash|shell|zsh|powershell|html|css|sass|less|"
    r"react|reactjs|vue|vuejs|angular|svelte|next\.js|nuxt|"
    r"django|flask|fastapi|rails|laravel|spring|express|"
    r"graphql|grpc|docker|kubernetes|terraform|git|"
    r"pandas|numpy|pytorch|tensorflow|jax|sklearn|scikit-learn|"
    r"tailwind(?:css)?|webpack|vite|babel|jest|pytest|"
    r"android|ios|unity|unreal|opengl|vulkan|webgl|wasm"
    r")\b",
    re.IGNORECASE,
)

# Programming structures, algorithms, paradigms, and architecture terms that
# are commonly the subject of a code sample. These only trigger coding mode
# together with an explicit code request or a named programming language, so
# generic uses of words such as "stack" or "architecture" do not match.
PROGRAMMING_CONCEPT_PATTERN = re.compile(
    r"\b(?:"
    r"linked\s+list|doubly\s+linked\s+list|binary\s+(?:tree|search\s+tree)|"
    r"b-?tree|trie|hash\s+(?:map|table|set)|stack|queue|deque|heap|"
    r"priority\s+queue|graph(?:\s+(?:traversal|algorithm))?|"
    r"depth[-\s]first\s+search|breadth[-\s]first\s+search|dfs|bfs|"
    r"dynamic\s+programming|memoization|recursion|recursive|backtracking|"
    r"greedy\s+algorithm|sorting\s+algorithm|binary\s+search|"
    r"quicksort|quick\s+sort|merge\s+sort|heapsort|heap\s+sort|"
    r"bubble\s+sort|insertion\s+sort|selection\s+sort|"
    r"polymorphism|inheritance|encapsulation|abstraction|"
    r"time\s+complexity|space\s+complexity|big[-\s]?o|data\s+structure|"
    r"design\s+pattern|singleton|factory\s+pattern|observer\s+pattern|"
    r"decorator|dependency\s+injection|dependency\s+inversion|"
    r"factory|builder|adapter|strategy|repository\s+pattern|mvc|mvvm|"
    r"event\s+loop|closure|promise|async(?:hronous)?|await|callback|"
    r"coroutine|thread(?:ing|s)?|concurrency|mutex|semaphore|race\s+condition|"
    r"garbage\s+collection|memory\s+leak|pointer|reference\s+count|"
    r"unit\s+test|integration\s+test|test[-\s]driven|mock(?:ing)?|"
    r"regular\s+expression|regex|orm|crud|rest\s+api|api\s+endpoint|"
    r"microservice|monolith|serverless|event[-\s]driven|message\s+queue|"
    r"pub(?:lish)?/?sub(?:scribe)?|load\s+balanc(?:er|ing)|cach(?:e|ing)|"
    r"database\s+index|schema|migration|transaction|acid|"
    r"object[-\s]oriented|functional\s+programming|generics?|interface|"
    r"abstract\s+class|class|function|method|module|package|library|"
    r"framework|compiler|interpreter|virtual\s+machine|bytecode|"
    r"frontend|backend|full[-\s]stack|architecture|system\s+design|"
    r"scalab(?:le|ility)|distributed\s+system|consensus|raft|paxos"
    r")\b",
    re.IGNORECASE,
)

# Unambiguous programming concepts. A question such as "what is dependency
# injection?" is a coding question even without an explicit language, while a
# generic word such as "stack" or "architecture" needs a code request or a
# named language before coding mode is used.
STRONG_PROGRAMMING_CONCEPT_PATTERN = re.compile(
    r"\b(?:"
    r"linked\s+list|doubly\s+linked\s+list|binary\s+(?:tree|search\s+tree)|"
    r"b-?tree|trie|hash\s+(?:map|table|set)|priority\s+queue|deque|"
    r"depth[-\s]first\s+search|breadth[-\s]first\s+search|dfs|bfs|"
    r"dynamic\s+programming|memoization|recursion|recursive|backtracking|"
    r"greedy\s+algorithm|sorting\s+algorithm|binary\s+search|"
    r"quicksort|quick\s+sort|merge\s+sort|heapsort|heap\s+sort|"
    r"bubble\s+sort|insertion\s+sort|selection\s+sort|"
    r"polymorphism|inheritance|encapsulation|abstraction|"
    r"time\s+complexity|space\s+complexity|big[-\s]?o|data\s+structure|"
    r"design\s+pattern|singleton|factory\s+pattern|observer\s+pattern|"
    r"dependency\s+injection|dependency\s+inversion|"
    r"repository\s+pattern|mvc|mvvm|"
    r"event\s+loop|closure|promise|async(?:hronous)?|await|callback|"
    r"coroutine|mutex|semaphore|race\s+condition|"
    r"garbage\s+collection|memory\s+leak|reference\s+count|"
    r"unit\s+test|integration\s+test|test[-\s]driven|mock(?:ing)?|"
    r"regular\s+expression|regex|orm|crud|rest\s+api|api\s+endpoint|"
    r"microservice|monolith|serverless|event[-\s]driven|message\s+queue|"
    r"pub(?:lish)?/?sub(?:scribe)?|load\s+balanc(?:er|ing)|"
    r"database\s+index|acid|object[-\s]oriented|functional\s+programming|"
    r"generics?|compiler|interpreter|virtual\s+machine|bytecode|"
    r"system\s+design|distributed\s+system|consensus|raft|paxos"
    r")\b",
    re.IGNORECASE,
)

# Language features and mechanics that are the subject of a programming
# question even when the question does not ask for code. Together with the
# concept patterns above, this lets a follow-up such as "how does ownership
# work?" keep consulting the Rust documentation from the previous turn.
PROGRAMMING_FEATURE_PATTERN = re.compile(
    r"\b(?:"
    r"ownership|borrow(?:ing|ed|s)?|borrow\s+checker|lifetimes?|"
    r"trait(?:s)?|macros?|goroutines?|channels?|defer|generics?|"
    r"pattern\s+matching|type\s+inference|memory\s+safety|"
    r"move\s+semantics|smart\s+pointers?|destructors?|drop\s+check|"
    r"async\s+runtime|iterators?|structs?|enums?|methods?|"
    r"error\s+handling|exceptions?|reflection|metaprogramming|"
    r"operator\s+overloading|zero[-\s]cost|compile[-\s]time"
    r")\b",
    re.IGNORECASE,
)

# Opinions and recommendations about a technology are not code requests,
# even when they mention a programming concept.
CODING_OPINION_PATTERN = re.compile(
    r"\b(?:what\s+do\s+you\s+think|do\s+you\s+(?:like|prefer|recommend)|"
    r"should\s+(?:i|we)\s+(?:use|choose|learn)|is\s+(?:it|\w+)\s+(?:worth|better)|"
    r"pros\s+and\s+cons|versus|vs\.?|compare|recommend(?:ation)?|"
    r"your\s+opinion|opinions?\s+on)\b",
    re.IGNORECASE,
)

CHINESE_CODING_PATTERN = re.compile(
    r"(?:代码|示例|例子|代码片段|函数|类|实现|编程|程序|架构|算法|"
    r"数据结构|写一个|写个|如何实现|怎么做|用\s*\w+\s*(?:写|实现))"
)

# Explicit requests for code / implementation help. Kept separate from the
# concept list so an ordinary question containing "class" or "function" is
# not misclassified.
CODE_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:show|write|give|provide|generate|create|implement|build|code|"
    r"debug|refactor|fix)\b.{0,60}\b(?:code|sample|snippet|example|function|"
    r"method|class|program|script|implementation|algorithm|data\s+structure|"
    r"query|api|endpoint|architecture|design)\b"
    r"|\b(?:code|sample|example|demo)\s+(?:code|sample|example|snippet|"
    r"implementation|program|for\s+(?:a|an|the))\b"
    r"|\bhow\s+(?:do|can|should|would|to)\s+(?:i|we|you)?\s*(?:implement|"
    r"build|write|code|create|structure|architect|design)\b"
    r"|\b(?:implement|implementing|code|program|architect)\s+(?:a|an|the|this|"
    r"that|my|our)\b"
    r"|\b(?:in|using|with)\s+(?:python|javascript|typescript|java|c\+\+|go|"
    r"rust|ruby|php|swift|kotlin|sql|bash|react|node(?:\.js)?)\b"
    r"|\b(?:explain|teach|demonstrate|walk\s+me\s+through)\b.{0,40}"
    r"\b(?:how\s+to|code|implementation|syntax|language\s+feature)\b"
    r")",
    re.IGNORECASE,
)

# Explanation questions about a language feature ("what is ownership in
# Rust?", "how does the borrow checker work?") are not code requests even
# though CODE_REQUEST_PATTERN's broad "in rust" alternative matches them. The
# server uses these patterns to keep such questions in the normal answer path,
# where the language documentation is combined with the transcript context.
PROGRAMMING_EXPLANATION_PATTERN = re.compile(
    r"^\s*(?:what\s+(?:is|are|does|do)|why\s+(?:is|are|does|do)|"
    r"how\s+(?:does|do|is|are)|explain|describe|tell\s+me\s+about|define|"
    r"definition\s+of|difference\s+between|when\s+(?:should|would|do)\s+"
    r"(?:i|we)\s+use|walk\s+me\s+through)\b",
    re.IGNORECASE,
)

EXPLICIT_CODE_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:show|write|give|provide|generate|create|implement|build|code|"
    r"debug|refactor|fix)\b.{0,60}\b(?:code|sample|snippet|example|function|"
    r"method|class|program|script|implementation|algorithm|data\s+structure|"
    r"query|api|endpoint|architecture|design)\b"
    r"|\b(?:code|sample|example|demo)\s+(?:code|sample|example|snippet|"
    r"implementation|program|for\s+(?:a|an|the))\b"
    r"|\bhow\s+(?:do|can|should|would|to)\s+(?:i|we|you)?\s*(?:implement|"
    r"build|write|code|create|structure|architect|design)\b"
    r"|\b(?:implement|implementing|code|program|architect)\s+(?:a|an|the|this|"
    r"that|my|our)\b)",
    re.IGNORECASE,
)

def programming_explanation_question(question: str) -> bool:
    """Return True for an explanatory question about a language feature."""
    text = " ".join(str(question or "").split())
    return bool(text and PROGRAMMING_EXPLANATION_PATTERN.search(text))

def explicit_code_request(question: str) -> bool:
    """Return True when the question explicitly asks for code or a program."""
    text = " ".join(str(question or "").split())
    return bool(
        text
        and (
            EXPLICIT_CODE_REQUEST_PATTERN.search(text)
            or CHINESE_CODING_PATTERN.search(question)
        )
    )

# A pasted code snippet is itself a strong signal that the question is a
# coding question, even when it is phrased as "why does this fail?".
CODE_SYNTAX_PATTERN = re.compile(
    r"(?:^|\n)\s*(?:def|class|function|const|let|var|import|from|"
    r"#include|public|private|protected|static|void|int|SELECT|CREATE|"
    r"INSERT|UPDATE|fn|func|package|using|async|await)\b"
    r"|=>|::|\{\s*\n|;\s*$",
    re.MULTILINE,
)

def question_requests_coding(question: str) -> bool:
    """Return True when a question asks for a programming code sample.

    Detection is deliberately conservative: a named language is only enough
    when the question also asks for code/design help or names a programming
    concept. Explicit code requests, pasted code, and language+concept
    questions are all handled without an extra model call.
    """
    text = " ".join(str(question or "").split())
    if not text:
        return False
    if CHINESE_CODING_PATTERN.search(question):
        return True
    if CODE_SYNTAX_PATTERN.search(question):
        return True
    if CODE_REQUEST_PATTERN.search(text):
        return True
    has_language = bool(PROGRAMMING_LANGUAGE_PATTERN.search(text))
    has_concept = bool(PROGRAMMING_CONCEPT_PATTERN.search(text))
    has_strong_concept = bool(STRONG_PROGRAMMING_CONCEPT_PATTERN.search(text))
    is_opinion = bool(CODING_OPINION_PATTERN.search(text))
    if is_opinion:
        return False
    return has_strong_concept or (has_language and has_concept)

def github_query_terms(
    question: str,
) -> tuple[list[str], str | None, tuple[str, ...] | None]:
    """Return GitHub search terms, language, and source extensions.

    The language is inferred from the programming-language names already used
    by coding-mode detection. Frameworks map to their implementation language
    (React -> JavaScript, Django -> Python). The returned terms remove the
    question's filler words and the language name so the query stays focused.
    """
    text = " ".join(str(question or "").split())
    language: str | None = None
    extensions: tuple[str, ...] | None = None
    raw_language = ""
    language_matches = list(GITHUB_EXTRA_LANGUAGE_PATTERN.finditer(text))
    language_matches.extend(PROGRAMMING_LANGUAGE_PATTERN.finditer(text))
    for match in sorted(language_matches, key=lambda item: item.start()):
        token = match.group(0).lower()
        mapped = GITHUB_LANGUAGE_MAP.get(token)
        if mapped is not None:
            language, extensions = mapped
            raw_language = token
            break

    language_tokens = set(chat.tokenize(raw_language))
    terms: list[str] = []
    for token in chat.tokenize(text):
        if len(token) < 2 or token.isdigit():
            continue
        if token in GITHUB_QUERY_STOPWORDS or token in language_tokens:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:GITHUB_MAX_QUERY_TERMS], language, extensions

def detect_code_language(question: str, fallback: str = "") -> str:
    """Return the programming language named by a question, if any.

    The GitHub language map already recognizes Rust, PHP, and "golang" as
    well as languages that are not documentation-backed. Only Go, Rust, and
    PHP are returned because those are the languages with an official-docs
    answer path. ``fallback`` keeps the previous language for a follow-up
    such as "now add error handling" when the new question does not name one.
    """
    language = detect_explicit_language(question)
    if language:
        return language if language in DOCUMENTATION_LANGUAGES else ""
    return fallback if fallback in DOCUMENTATION_LANGUAGES else ""

def detect_explicit_language(question: str) -> str:
    """Return any programming language explicitly named by a question.

    Unlike :func:`detect_code_language`, this also returns languages such as
    Python or JavaScript that have no official-docs integration. Callers use
    that to avoid treating a follow-up in a previous Rust conversation as a
    Rust question when the new question clearly switches to another language.
    """
    text = " ".join(str(question or "").split())
    _terms, language, _extensions = github_query_terms(text)
    if language:
        return language
    if re.search(
        r"\b(?:in|with|using)\s+go\b|\bgo\s+(?:program|code|script|cli|"
        r"function|package|module|programming)\b",
        text,
        re.IGNORECASE,
    ):
        return GO_LANGUAGE
    return ""

def programming_feature_question(question: str) -> bool:
    """Return True when a question is about a language or language feature.

    This is broader than coding-mode detection: a question such as "how does
    ownership work?" asks about a Rust feature without requesting code. It is
    used only to decide whether a follow-up should keep the previous language
    for model/fence selection; it never causes documentation to be loaded.
    """
    text = " ".join(str(question or "").split())
    if not text:
        return False
    return bool(
        question_requests_coding(question)
        or CODE_REQUEST_PATTERN.search(text)
        or CHINESE_CODING_PATTERN.search(question)
        or PROGRAMMING_CONCEPT_PATTERN.search(text)
        or STRONG_PROGRAMMING_CONCEPT_PATTERN.search(text)
        or PROGRAMMING_FEATURE_PATTERN.search(text)
    )

def programming_docs_language(question: str, fallback: str = "") -> str:
    """Return the documentation-backed language for a language question.

    An explicitly named language wins. When the question does not name one,
    the previous programming language is reused only when the new question is
    clearly about a language or one of its features. This prevents an
    unrelated question from inheriting stale documentation context.
    """
    explicit = normalize_language(detect_explicit_language(question))
    if explicit:
        return explicit if explicit in PROGRAMMING_DOCS_MAP else ""
    if not programming_feature_question(question):
        return ""
    previous = normalize_language(fallback)
    return previous if previous in PROGRAMMING_DOCS_MAP else ""

def _github_headers(text_match: bool = False) -> dict[str, str]:
    headers = dict(GITHUB_HEADERS)
    if text_match:
        # Ask the code-search endpoint for the matched fragments, not just
        # file paths. Without this header every result would need a second
        # request to download the file.
        headers["Accept"] = "application/vnd.github.text-match+json"
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .config import CODING_MODE_MODEL, CODING_MODE_PROMPT_PREFIX, DOCUMENTATION_LANGUAGES, GITHUB_HEADERS, GITHUB_MAX_QUERY_TERMS, GITHUB_TOKEN, GO_LANGUAGE, LANGUAGE_CODE_FENCES, PHP_LANGUAGE, PROGRAMMING_DOCS_MAP, PROGRAMMING_MODEL, RUST_LANGUAGE
