"""GitHub code-snippet fallback retrieval."""

from __future__ import annotations

from .core import *
from .config import GITHUB_SNIPPET_LIMIT

def _github_snippet_language(path: str) -> str:
    suffix = Path(path).suffix.lower()
    for language, extensions in GITHUB_LANGUAGE_MAP.values():
        if suffix in extensions:
            return language
    return ""

def _github_trim_snippet(text: str, terms: list[str]) -> str:
    """Trim a source file or search fragment down to a readable snippet."""
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        return ""
    all_lines = normalized.split("\n")
    if len(all_lines) > GITHUB_SNIPPET_MAX_LINES:
        lowered = [line.lower() for line in all_lines]
        anchor: int | None = None
        for term in terms:
            needle = term.lower()
            if not needle:
                continue
            for index, line in enumerate(lowered):
                if needle in line:
                    anchor = index
                    break
            if anchor is not None:
                break
        start = max(0, (anchor if anchor is not None else 0) - 6)
        # Move the start to a nearby declaration/import so the snippet begins
        # at a natural boundary instead of in the middle of an expression.
        for index in range(start, min(len(all_lines), start + 12)):
            if re.match(
                r"\s*(?:def|class|function|func|fn|pub\s+fn|public|private|"
                r"protected|static|void|int|const|let|var|#include|import|"
                r"from|package|using|SELECT|CREATE)\b",
                all_lines[index],
            ):
                start = index
                break
        end = min(len(all_lines), start + GITHUB_SNIPPET_MAX_LINES)
        snippet = "\n".join(all_lines[start:end]).strip()
        if start > 0:
            snippet = "...\n" + snippet
        if end < len(all_lines):
            snippet += "\n..."
    else:
        snippet = normalized.strip()

    if len(snippet) > GITHUB_SNIPPET_MAX_CHARS:
        snippet = snippet[:GITHUB_SNIPPET_MAX_CHARS].rstrip()
        snippet += "\n... (truncated)"
    return snippet

def _github_raw_file(repo: str, branch: str | None, path: str) -> str:
    """Download a text source file from raw.githubusercontent.com."""
    if not repo or not path:
        return ""
    ref = branch or "HEAD"
    url = (
        f"{GITHUB_RAW_BASE}/{quote(repo, safe='/')}/"
        f"{quote(ref, safe='/')}/{quote(path, safe='/')}"
    )
    try:
        response = requests.get(
            url,
            headers={"User-Agent": GITHUB_HEADERS["User-Agent"]},
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return ""
    if response.status_code != 200:
        return ""
    if len(response.content) > GITHUB_MAX_FILE_BYTES:
        return ""
    # Skip binaries such as images or compiled artifacts.
    if b"\x00" in response.content:
        return ""
    return response.text

def _github_repo_code_paths(
    repo: str,
    branch: str,
    extensions: tuple[str, ...] | None,
    terms: list[str],
    deadline: float,
) -> list[str]:
    """List candidate source files in a repository, best match first."""
    if time.monotonic() >= deadline:
        return []
    url = (
        f"{GITHUB_API_BASE}/repos/{quote(repo, safe='/')}/git/trees/"
        f"{quote(branch or 'HEAD', safe='')}"
    )
    try:
        response = requests.get(
            url,
            params={"recursive": "1"},
            headers=_github_headers(),
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []
    tree = data.get("tree")
    if not isinstance(tree, list):
        return []

    suffixes = extensions or GITHUB_DEFAULT_EXTENSIONS
    normalized_terms = [
        re.sub(r"[^a-z0-9]+", "", term.lower()) for term in terms if term
    ]
    scored: list[tuple[int, int, str]] = []
    for item in tree:
        if not isinstance(item, dict) or item.get("type") != "blob":
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        if Path(path).suffix.lower() not in suffixes:
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > GITHUB_MAX_FILE_BYTES:
            continue
        lowered = path.lower()
        normalized_path = re.sub(
            r"[^a-z0-9]+", "", Path(path).with_suffix("").as_posix().lower()
        )
        score = sum(
            2
            for term in normalized_terms
            if term and term in normalized_path
        )
        if re.search(
            r"(?:^|/)(?:tests?|vendor|node_modules|examples?|docs?)/",
            lowered,
        ):
            score -= 1
        scored.append((score, -len(path), path))

    scored.sort(reverse=True)
    matching = [path for score, _length, path in scored if score > 0]
    if matching:
        return matching[:5]
    return [path for _score, _length, path in scored[:3]]

def _github_code_search_snippets(
    query: str,
    language: str | None,
    extensions: tuple[str, ...] | None,
    terms: list[str],
    limit: int,
    deadline: float,
) -> list[dict]:
    """Search GitHub code (requires a token) and return matched fragments."""
    if not GITHUB_TOKEN or limit <= 0 or time.monotonic() >= deadline:
        return []
    search_query = f"{query} in:file"
    if language:
        search_query += f" language:{language}"
    try:
        response = requests.get(
            f"{GITHUB_API_BASE}/search/code",
            params={"q": search_query, "per_page": max(limit * 4, 10)},
            headers=_github_headers(text_match=True),
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []

    snippets: list[dict] = []
    seen: set[str] = set()
    for item in data.get("items") or []:
        if len(snippets) >= limit:
            break
        if not isinstance(item, dict):
            continue
        repository = item.get("repository") or {}
        repo = str(repository.get("full_name") or "")
        path = str(item.get("path") or "")
        url = str(item.get("html_url") or "")
        if not url or url in seen:
            continue
        pieces: list[str] = []
        for match in item.get("text_matches") or []:
            if not isinstance(match, dict):
                continue
            fragment = str(match.get("fragment") or "")
            if fragment.strip():
                pieces.append(fragment)
        code = _github_trim_snippet("\n...\n".join(pieces), terms)
        if not code and repo and path:
            code = _github_trim_snippet(
                _github_raw_file(repo, None, path), terms
            )
        if not code:
            continue
        seen.add(url)
        snippets.append(
            {
                "repo": repo or "GitHub",
                "path": path or "(code search result)",
                "url": url,
                "language": language or _github_snippet_language(path),
                "code": code,
            }
        )
    return snippets

def _github_repository_search_snippets(
    query: str,
    language: str | None,
    extensions: tuple[str, ...] | None,
    terms: list[str],
    limit: int,
    deadline: float,
) -> list[dict]:
    """Unauthenticated fallback: find repositories and read source files."""
    if limit <= 0 or time.monotonic() >= deadline:
        return []
    search_query = query
    if language:
        search_query += f" language:{language}"
    try:
        response = requests.get(
            f"{GITHUB_API_BASE}/search/repositories",
            params={
                "q": search_query,
                "sort": "stars",
                "order": "desc",
                "per_page": GITHUB_REPOSITORY_LIMIT,
            },
            headers=_github_headers(),
            timeout=GITHUB_TIMEOUT,
        )
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []
    try:
        data = response.json()
    except ValueError:
        return []

    snippets: list[dict] = []
    seen: set[str] = set()
    for repo_data in data.get("items") or []:
        if len(snippets) >= limit or time.monotonic() >= deadline:
            break
        if not isinstance(repo_data, dict):
            continue
        repo = str(repo_data.get("full_name") or "")
        branch = str(repo_data.get("default_branch") or "HEAD")
        if not repo:
            continue
        paths = _github_repo_code_paths(
            repo, branch, extensions, terms, deadline
        )
        for path in paths:
            if len(snippets) >= limit or time.monotonic() >= deadline:
                break
            url = f"https://github.com/{repo}/blob/{branch}/{path}"
            if url in seen:
                continue
            code = _github_trim_snippet(
                _github_raw_file(repo, branch, path), terms
            )
            if not code:
                continue
            seen.add(url)
            snippets.append(
                {
                    "repo": repo,
                    "path": path,
                    "url": url,
                    "language": language or _github_snippet_language(path),
                    "code": code,
                }
            )
            # One snippet per repository keeps the two sources diverse.
            break
    return snippets

def github_code_snippets(
    question: str,
    limit: int = GITHUB_SNIPPET_LIMIT,
    log: Callable[[str], None] | None = None,
    language_guard: str | None = None,
) -> list[dict]:
    """Return up to ``limit`` GitHub code snippets related to ``question``.

    GitHub's code-search API needs a token. With one, matched fragments come
    back directly. Without one, repository search plus the public git-tree and
    raw-content endpoints provide the same fallback using only anonymous
    requests. Results are deduplicated by URL.

    ``language_guard`` is a hard safety guard: Go, Rust, and PHP questions
    never reach GitHub, even if a future caller forgets to check first.
    """

    def emit(message: str) -> None:
        if log is not None:
            log(message)

    if not github_code_fallback_allowed(language_guard):
        emit(
            f"[github: code search disabled for {language_guard}; "
            "official language documentation is used instead]"
        )
        return []

    terms, language, extensions = github_query_terms(question)
    if not terms:
        if not language:
            return []
        terms = [language.lower()]

    deadline = time.monotonic() + GITHUB_TOTAL_TIMEOUT
    query_candidates: list[str] = []
    for count in (len(terms), min(3, len(terms)), 1):
        candidate = " ".join(terms[:count]).strip()
        if candidate and candidate not in query_candidates:
            query_candidates.append(candidate)

    snippets: list[dict] = []
    seen: set[str] = set()
    for query in query_candidates[:2]:
        if len(snippets) >= limit or time.monotonic() >= deadline:
            break
        label = query + (f" ({language})" if language else "")
        emit(f"[github: searching code for {label!r}]")
        found = _github_code_search_snippets(
            query,
            language,
            extensions,
            terms,
            limit - len(snippets),
            deadline,
        )
        if found:
            if len(found) < limit - len(snippets):
                found.extend(
                    _github_repository_search_snippets(
                        query,
                        language,
                        extensions,
                        terms,
                        limit - len(snippets) - len(found),
                        deadline,
                    )
                )
        else:
            emit("[github: code search unavailable; searching repositories]")
            found = _github_repository_search_snippets(
                query,
                language,
                extensions,
                terms,
                limit - len(snippets),
                deadline,
            )
        for snippet in found:
            url = str(snippet.get("url") or "")
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            snippets.append(snippet)
            if len(snippets) >= limit:
                break
    return snippets[:limit]

def format_github_code_answer(snippets: list[dict]) -> str:
    """Build a Markdown answer from the retrieved GitHub code snippets."""
    lines = [
        "I could not find a complete answer in the local knowledge base, so I "
        "searched GitHub for related code. Here are the most relevant "
        "snippets:",
        "",
    ]
    for number, snippet in enumerate(snippets, start=1):
        repo = str(snippet.get("repo") or "GitHub")
        path = str(snippet.get("path") or "")
        url = str(snippet.get("url") or "")
        label = f"{repo}/{path}" if path else repo
        if url:
            lines.append(f"### Snippet {number}: [{label}]({url})")
        else:
            lines.append(f"### Snippet {number}: {label}")
        lines.append("")
        language = str(snippet.get("language") or "").strip()
        lines.append(f"```{language}" if language else "```")
        lines.append(str(snippet.get("code") or "").strip())
        lines.append("```")
        lines.append("")
    lines.append(
        "These snippets are starting points; adapt them to your project's "
        "types, imports, and error handling."
    )
    return "\n".join(lines).strip() + "\n"


# Cross-component imports are placed after the definitions so a
# feature module can depend on another feature module safely.
from .config import GITHUB_API_BASE, GITHUB_HEADERS, GITHUB_MAX_FILE_BYTES, GITHUB_RAW_BASE, GITHUB_REPOSITORY_LIMIT, GITHUB_SNIPPET_MAX_CHARS, GITHUB_SNIPPET_MAX_LINES, GITHUB_TIMEOUT, GITHUB_TOKEN, GITHUB_TOTAL_TIMEOUT
from .programming import GITHUB_DEFAULT_EXTENSIONS, GITHUB_LANGUAGE_MAP, _github_headers, github_code_fallback_allowed, github_query_terms
