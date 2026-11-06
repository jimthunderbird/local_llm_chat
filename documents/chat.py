#!/usr/bin/env python3
"""Documentation: how ./chat.py turns a question into a grounded answer.

This file is documentation, not part of the runtime. It can be printed with:

    python3 documents/chat.py

It describes the implementation in ./chat.py as of 2026-10-28. The chatbot is
an interactive, local, lexical (BM25) RAG system over YouTube transcript
"trunks". It deliberately does NOT use embeddings, cosine similarity, or a
vector database.

================================================================================
1. HIGH-LEVEL SHAPE
================================================================================

The pipeline is:

    question
      -> (optional) Chinese -> English translation, used only for retrieval
      -> answer-cache lookup
      -> query expansion with qwen3:1.7b (one short line of synonyms)
      -> BM25 over the loaded trunks (original terms weighted 2x + expansion)
      -> top-N by depth, or adaptive selection in "auto" mode
      -> optional small-LLM question filter (qwen3:1.7b)
      -> soft tag scope: keep candidates relevant to tag + question
      -> optional tag routing if no candidate fits the active tag
      -> numbered <chunk> context blocks
      -> prompt from research/100_trunks_prompt.txt
      -> streaming answer from ornith:35b over Ollama
      -> streaming filters remove chunk citations and "Supporting Evidence"
      -> answer cached; sources and benchmark metrics retained for commands

There are two distinct data scopes:

    tag scope        one global corpus, scoped at query time by ./tags.toml
    transcript scope trunks of one video only, selected by /transcript
    article scope    paragraphs of one local article, selected by /article

The default tag is "japan". ./tags.toml is the tag registry: each tag has a
description and optional keywords. Tags do not partition the corpus and are
never assigned to videos during ingestion. No embeddings are loaded at any
point.

================================================================================
2. STARTUP PATH
================================================================================

main() in ./chat.py:

1. parse_args()
     --tag <tag>              default: japan
                              "--tag" without a value -> interactive picker
     --max-context-tokens N   adaptive-retrieval token budget (default 4096)
     --progressive            optional two-pass streaming (default off)

2. A daemon thread calls prewarm(), which sends a one-token request to
   qwen3:1.7b and ornith:35b with keep_alive="30m". This moves model loading
   off the critical path. Failure is ignored.

3. load_all_chunks():
     - globs ./youtube_transcript/*.trunk
     - keeps every <video_id>.<index>.trunk file (or only an explicit
       /transcript video set when that scope is active)
     - builds Chunk(video_id, index, trunk_path) objects and sorts them by
       (video_id, index)
     - no text is read and no BM25 index is built yet
     - /tag changes only the soft tag context; it does not reload the corpus

   Chunk.label is "<video_id>.<index>". Text is read lazily, and cached:

     paragraph_text(chunk)  -> reads <paragraph>...</paragraph> from a trunk
                               through an 8,192-entry LRU (per chunk)
     video_title(video_id)  -> reads <video_id>.title through a
                               4,096-entry LRU (one entry per video)

4. PlainFileHistory loads ./.chat/question_history.txt, one entry per line.
   Questions and slash commands are both saved, so Up/Down navigates the full
   session history. prompt_toolkit supplies vi key bindings and the toolbar
   (current tag, mode, key hints). Tab completion is registered for commands,
   tag names, video ids, and /transcript title matches.

5. The banner prints the loaded-chunk count, the current tag, the default
   depth, the prompt file, and the command list.

================================================================================
3. PER-QUESTION WORKFLOW IN DETAIL
================================================================================

3.1 Input and command dispatch
------------------------------

session.prompt("You: ") returns a line. Empty lines are ignored. A line that
starts with "/" is dispatched as a command (see section 5). Otherwise it is a
question and goes to answer().

3.2 Simplified Chinese detection
--------------------------------

contains_simplified_chinese() looks for Han characters, rejects text that
contains kana (Japanese), and compares counts of simplified-only and
traditional-only character sets. If the question is Simplified Chinese:

    - the question is translated to English with qwen3:1.7b for retrieval;
      the translation prompt explicitly tells the model to romanize Japanese
      place names instead of reading Chinese characters;
    - answer_language is set to "Simplified Chinese", which adds an
      "Answer in Simplified Chinese" instruction to the final prompt.

The answer is produced directly in Chinese by ornith:35b. There is no
post-hoc answer translation in the default path (translate_answer=False).
A streaming Chinese translation path still exists in stream_answer() for
callers that request it.

3.3 Cache lookup
----------------

make_cache_key() includes:

    tag, transcript scope, depth, max_context_tokens, answer language,
    progressive flag, and the normalized question

AnswerCache is a 32-entry LRU. On a hit the cached answer and source list are
returned without retrieval or model calls; benchmark stats are filled with
zeroes and cached=True. The cache is cleared by /tag, /refresh, /transcript,
/youtube learn, and /depth changes.

3.4 Query expansion
-------------------

expand_query() sends the (possibly translated) question to qwen3:1.7b with
temperature 0.1, num_predict=32, and stop sequences at newlines. The model is
asked for 4-8 synonyms / related terms on one line. Think blocks are stripped,
punctuation is removed, and the result is capped at 500 characters. Failure is
non-fatal; retrieval then uses the original question only.

3.5 BM25 retrieval
------------------

The BM25 index is built lazily on the first question and cached in _BM25_CACHE
(two entries, keyed by id(chunks)) so the global corpus index and an explicit
transcript/article index can coexist.

build_bm25_index():

    for each chunk:
        document = "<video title>\\n<paragraph text>"
        tokenized_documents.append(tokenize(document))

tokenize() is a lowercase Unicode word-boundary split: regex \\b\\w+\\b.
rank_bm25.BM25Okapi then computes document frequencies, IDF, and document
lengths in memory.

top_chunks():

    query_tokens = tokenize(search_question) * 2 + tokenize(expansion)

The original question terms are duplicated, so they outweigh expansion terms.
BM25 scores are computed for every chunk. Non-positive scores are discarded,
then results are sorted descending.

Selection then depends on depth:

  Fixed depth (default 20)
      Return the top `depth` chunks, score > 0 only. /depth <n> changes it.

  Adaptive depth (/depth auto)
      _select_auto_chunks() applies deterministic rules:

      1. Consider at most AUTO_CANDIDATE_LIMIT = 50 candidates.
      2. Always keep at least min(FALLBACK_TOP_N=10, available) chunks.
      3. After that minimum, stop when the score falls below 15% of the best
         score, or when the score drops more than 35% from the previous score
         (the "elbow").
      4. At most MAX_CHUNKS_PER_VIDEO = 5 chunks may come from one video.
      5. Drop near-duplicates when the Jaccard overlap of token sets with an
         already-selected chunk is >= 0.90.
      6. Stop when the estimated token budget is reached (4,096 tokens by
         default; 6,144 for list/aggregation questions). A chunk's token cost
         is estimate_tokens(paragraph) + 16.
      7. If everything was filtered out, keep the single best chunk.

3.6 Small-model relevance filter
--------------------------------

The filter is applied only when:

    LLM_FILTER_ENABLED is true (it is),
    the question is NOT a list/aggregation question, and
    5 <= number of BM25 candidates <= 20.

The filter prompt shows each candidate number plus a snippet capped at 700
characters. qwen3:1.7b must return a comma-separated list of useful candidate
numbers, or NONE. The model gets at most 96 output tokens and temperature 0.
If it returns NONE, the candidate set is considered empty and soft-tag routing
may run. If its output cannot be parsed or the request fails, the question
filter is treated as skipped and the BM25 candidates are kept; the tag scope
has its own separate status and lexical fallback.

List questions (list/all/every/each/compare/details/types/levels/options/...)
skip the filter because exhaustive answers need broad coverage.

3.7 Cross-tag fallback
----------------------

The active tag is a soft scope over the one global corpus. After the question
candidates are retrieved, tag_filter_candidates() asks qwen3:1.7b to keep only
candidates relevant to both the tag description/keywords and the question. If
the model returns NONE, the CLI/server ranks the other registry tags from the
same candidate set (lexical tag profiles) and re-runs retrieval for the best
tag(s), switching only after a scoped answer succeeds. If no tag fits, the UI
says so instead of silently citing unrelated chunks. Fallback is disabled for
an explicit /transcript or /article scope.

If the filter was too strict but BM25 did return candidates, the code keeps
the best FILTER_MIN_KEEP = 3 unfiltered candidates instead of refusing.

3.8 Context construction
------------------------

build_context() emits numbered blocks:

    <chunk id="1">
    [Video: <title>]
    <paragraph text>
    </chunk>
    <chunk id="2">
    <paragraph text from a different video: title NOT repeated>
    </chunk>
    ...

The video title is emitted once per video, before its first chunk. Chunk ids
are internal metadata only; the prompt forbids mentioning them, and streaming
filters remove them if the model disobeys. Chunks whose paragraph text cannot
be read are skipped.

estimate_tokens() is a cheap approximation: ASCII characters / 4 (rounded up)
plus one token per non-ASCII character. It is used for the context-size line
and for the adaptive budget, not for billing.

After selection, the chatbot prints:

    [using N of M BM25 trunk(s) (depth D, default/explicit)]
    [context size: ~T token(s) estimated across N trunk(s);
     context window: 32,768 token(s)]

In auto mode the first line becomes "[auto retrieval: ...]".

3.9 Prompt construction
-----------------------

build_prompt() loads research/100_trunks_prompt.txt (falling back to a
built-in equivalent if the file is missing). It:

    - replaces the template's <context>...</context> block with the numbered
      chunk blocks;
    - replaces "[Insert your question here]" with the question;
    - changes "the 100 chunks" to the actual chunk count when present;
    - when answer_language is set, inserts "Answer in <language>. Do not
      translate proper nouns." immediately before "### TARGET QUESTION:";
    - prepends prompt_prefix if one is supplied.

The template enforces grounding in the provided chunks, no chunk citations,
conflict disclosure, deduplication for counts, and an exact missing-data
sentence. The model is asked for a direct synthesis with no separate evidence
section.

3.10 Answer generation and streaming
------------------------------------

stream_answer() POSTs to Ollama's /api/generate with:

    model:       ornith:35b (CHAT_MODEL)
    stream:      true
    think:       false
    keep_alive:  30m
    temperature: 0.2
    num_ctx:     32,768

Each NDJSON line is parsed and its "response" token is passed through two
stateful streaming filters before display:

    ChunkCitationFilter
        removes forms such as [Chunk 11], [Chunks 11, 14], or [chunk #3],
        including partial citations split across tokens; it holds ambiguous
        "[" prefixes until they can be resolved.

    SupportingEvidenceFilter
        suppresses a trailing "Supporting Evidence" section, including
        Markdown headings, bold markers, bullets, and blockquotes, even when
        the heading is split across tokens. The same section is also stripped
        from the final string.

The full English answer is accumulated and returned. If translation is
requested, sentence/line groups are translated on the fly by qwen3:1.7b and
printed in Simplified Chinese; list markers are restored if the small model
drops them. The default chatbot path does not enable this because it asks
ornith:35b to answer in Chinese directly.

Ollama's final event supplies load_duration, prompt_eval_count,
prompt_eval_cached_count, prompt_eval_duration, eval_count, and eval_duration.
The first displayed token time is recorded as request TTFT. The end-to-end
TTFT is retrieval time + request TTFT.

3.11 Optional progressive mode (--progressive)
-----------------------------------------------

If more than 10 chunks were selected, the first 10 are sent in one streaming
call. Then a second call receives the remaining chunks and the answer so far,
with instructions to add only missing details without repetition. The two
responses are concatenated. This trades a little coherence for a faster first
token. It is off by default.

3.12 Post-processing, caching, and reporting
--------------------------------------------

The returned answer is passed through strip_chunk_citations() and
strip_supporting_evidence() once more. It is stored in the answer cache along
with the selected (chunk, BM25 score) sources and the estimated context token
count. The latest answer, sources, and benchmark stats are retained for /dict,
/sources, and /benchmark.

By default no metrics line is printed. With /benchmark (or /benchmark on),
format_benchmark_line() prints retrieval, expansion, BM25, filter, context
build, prompt build, prefill, model load, TTFT, prompt tokens (and cached
tokens), estimated context tokens, and total time.

================================================================================
4. DATA STRUCTURES AND CACHES
================================================================================

Chunk
    video_id: str, index: int, trunk_path: Path. Very small; text is not held
    in the object.

BM25Index
    chunks: list[Chunk]
    tokenized_documents: list[list[str]]  (all terms, all documents)
    bm25: BM25Okapi

_BM25_CACHE
    OrderedDict with at most 2 indexes: global corpus and an explicit scope.
    Keyed by id(chunks), with an identity check to avoid stale id reuse.

Paragraph/title caches
    @lru_cache(maxsize=8192) for paragraph bodies
    @lru_cache(maxsize=4096) for video titles
    These avoid re-reading 5,000+ small files during retrieval, but they also
    hold the corresponding text strings in memory.

AnswerCache
    At most 32 complete answers, with sources and context token counts.

_ALL_TAGGED_CHUNKS / load_all_tagged_chunks()
    Compatibility aliases; the tagged union is now simply the global corpus.

History
    Plain-text file, no in-memory index.

================================================================================
5. COMMANDS
================================================================================

/memory                 Show trunk/video counts, tokenized term count, peak RSS.
/tag [tag]              Switch tag; reload trunks, release old ones, clear cache.
/refresh                Reload the current tag's trunks.
/transcript <hash|url>  Use one video's trunks (explicit scope, no fallback).
/transcript <keyword>   Search *.title with ag, then choose from a hint list.
/search_transcript ...  Alias for the keyword form.
/youtube learn <hash|url>
                       Run fetch -> rewrite -> chunk -> embed -> tag; reload
                        the new video if it belongs to the current scope.
/wiki <word>            Wikipedia REST summary.
/dict <word>            Local-model dictionary entry; also explains the word's
                        use in the latest answer when it appears there.
/depth <n|auto|default> Fixed top-N BM25 selection or adaptive selection.
/status                 Tag, scope, loaded counts, retrieval mode, cache, history.
/sources                Source chunks and BM25 scores for the latest answer.
/benchmark [on|off]     Show one metrics line, or toggle per-answer metrics.
/exit, /quit, /q        Leave.

Tab completes commands, tag names, and video ids. Commands that take an
argument get an automatic trailing space. /dict no longer calls an external
dictionary API; it uses gemma3:1b.

================================================================================
6. MEMORY USAGE (MEASURED)
================================================================================

These numbers are for the current repository on macOS (Python 3, CPython),
measured with ps RSS, resource.getrusage, tracemalloc, and recursive
sys.getsizeof() over the index structures. They exclude Ollama, which runs in
separate processes and holds the model weights (qwen3:1.7b, ornith:35b,
gemma3:1b) outside the Python memory footprint.

Corpus in this checkout
-----------------------

    ./youtube_transcript/*.trunk files:        5,348
    union of all tagged videos:                4,959 trunks / 211 videos
    tag japan:                                 2,916 trunks / 124 videos
    tag ai_and_coding:                         1,719 trunks /  76 videos
    tag artificial_intelligence:               1,932 trunks /  81 videos
    total trunk text (tagged union):           ~1.24 MiB of characters
    tag japan text:                            ~0.69 MiB of characters
    average trunk paragraph:                   ~248 characters, ~62 est. tokens
    tag japan BM25 tokenized terms:            170,702
    all-tagged BM25 tokenized terms:           298,330

Measured process memory
-----------------------

    Python + imports, before loading trunks:   ~52-60 MiB
    after loading 2,916 japan chunks:          +7-14 MiB
    after building the japan BM25 index:       +14-25 MiB
    typical total, tag japan, first question:  ~75-100 MiB
    after loading/indexing all tagged chunks:  ~110-130 MiB
    peak observed with both indexes resident:  ~128 MiB

The range reflects Python allocator behavior, ps sampling, and whether
tracemalloc was active. In a clean run the tag-japan total was about 74-97
MiB; the all-tagged run reached about 128 MiB.

Where the index memory goes (tag japan, 2,916 chunks)
------------------------------------------------------

    tokenized_documents (all term strings/lists): ~8.9 MiB (deep size)
    BM25Okapi.doc_freqs (per-document term counters): ~10.3 MiB
    BM25Okapi.idf dictionary:                     ~0.9 MiB
    BM25Okapi.doc_len:                            ~0.03 MiB
    Chunk objects and paths:                      ~0.16 MiB
    cached paragraph text (~723k chars):          ~1.5-2 MiB
    other interpreter/list overhead:              remainder of the RSS delta

The two dominant costs are the tokenized corpus and rank_bm25's per-document
term-frequency counters. BM25 has no vector matrix, so memory scales with
total token count, not with an embedding dimension.

Rules of thumb
--------------

    per loaded chunk (text + caches + index):     ~5-8 KiB
    per 1,000 chunks, fully indexed:              ~5-8 MiB
    default tag japan (2,916 chunks):             ~75-100 MiB total process
    all 4,959 tagged chunks:                      ~110-130 MiB total process
    answer cache (32 answers + sources):          usually < 1-3 MiB
    global corpus index (10,660 chunks):          ~60 MiB index + text caches

The global corpus is loaded once. /tag changes only the soft tag metadata;
/refresh and /youtube learn reload the corpus and clear the BM25 cache, but the
paragraph/title LRU caches persist, so a refresh does not immediately return
all text memory to the OS.

Why memory stays moderate
-------------------------

    - No embeddings and no cosine-similarity matrix.
    - Trunk text is read lazily and only the paragraph body is cached.
    - One BM25 index per active scope, at most two cached indexes.
    - Cross-tag data is lazy and signature-invalidated.
    - The answer cache is bounded to 32 entries.
    - Models live in Ollama, not in the chatbot process.

================================================================================
7. FAILURE BEHAVIOR AND SAFETY NETS
================================================================================

    - Ollama unreachable: generate/stream raises RuntimeError; main prints
      "Error: ..." and keeps the session alive.
    - Query expansion failure: warning, retrieval uses the original question.
    - Filter failure/unparseable output: keep unfiltered BM25 candidates.
    - Tag scope returns NONE: route to the best other tag from tags.toml
      (unless in transcript/article scope); if no tag fits, explain the scope.
    - No trunks in scope: load_chunks exits with a hint to run youtube_video_learn.py.
    - Trunk read failure in context build: that chunk is skipped; if no text
      remains, answer() reports that it could not read matching contents.
    - Model leaks chunk citations or a Supporting Evidence section: streaming
      filters and a final post-pass remove them.
    - Answer cache is keyed by tag, scope, depth, token budget, language,
      progressive flag, and normalized question, so stale answers are not
      reused after a configuration or scope change.

================================================================================
8. QUICK REFERENCE
================================================================================

    Run:                  python3 chat.py [--tag <tag>]
    Pick a tag:           python3 chat.py --tag
    Adaptive retrieval:   /depth auto
    Fixed depth:          /depth 40
    Single transcript:    /transcript <hash|url|keyword>
    Learn a video:        /youtube learn <hash|url>   (or python3 youtube_video_learn.py ...)
    Source list:          /sources
    Metrics:              /benchmark [on|off]
    Memory report:        /memory
"""

if __name__ == "__main__":
    print(__doc__)
