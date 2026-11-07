# Speeding Up Time-to-First-Token in `chat.py`

Measurement-driven research and implementation plan.

Date: 2026-10-27

## 1. Executive summary

On this machine, with the default settings (`/depth 100`), a warm question costs
about **9.5 seconds** from pressing Enter to the first streamed answer token.
A cold question costs about **10.5-11.4 seconds**. The time is **not** dominated
by loading embeddings or by cosine similarity. It is dominated by two things:

1. **Prompt prefill**: roughly 8,900 prompt tokens at depth 100 take about
   7.7 s to process before the model can emit its first token.
2. **The per-query paragraph re-ranking call**: `top_chunks()` re-embeds 100
   trunk paragraphs through Ollama on every question, which costs about 1.8 s.

The highest-leverage fixes, in order, are:

| Fix | Expected effect | Effort |
|---|---|---|
| Precompute paragraph embeddings offline and remove the per-query re-embed | retrieval 1.8 s -> ~0.02 s | medium |
| Cap context by token budget instead of always using 100 chunks | prefill 7.7 s -> ~2-3 s | low |
| Stop repeating the video title in every chunk's prompt text | ~30-40% fewer prompt tokens | low |
| Preload models and set `keep_alive` | removes ~1-2 s cold-start | low |
| Avoid the blocking Chinese translation calls | removes ~1-1.6 s for Chinese questions | medium |

A realistic target while keeping the same chat model and answer quality is
**2-3 seconds end-to-end** at a 3-4k-token context. An aggressive two-stage
streaming design (start from 8-10 chunks, stream the first paragraph, then
expand) can reach **under 1 second perceived TTFT**, at the cost of a more
complex answer pipeline.

The most important structural finding is that **`DEFAULT_DEPTH = 100` is
incompatible with low TTFT**. At depth 100 the retrieved set is also highly
redundant: in the measured wagyu query, 100 chunks came from only 16 videos,
28 chunks (32% of context tokens) came from one video, and only 15 chunks had a
score above the 0.75 threshold. The extra 85 chunks added mostly low-scoring,
overlapping material.

## Action Plan

This section is the executable plan. It turns the findings above into a
sequence of independently shippable steps. Implement them in order; each step
has its own verification gate so a regression can be attributed to one change.

Baseline to beat: **~9.5 s warm end-to-end TTFT** at `DEFAULT_DEPTH = 100`
(retrieval ~1.8 s + prefill ~7.7 s). Target after step 4: **~2-3 s**. Target
after step 9: **<1 s perceived TTFT** for streaming questions.

### At a glance

| Step | Change | Main file(s) | Cumulative target |
|---:|---|---|---|
| 0 | Add TTFT/prefill instrumentation | `chat.py` | measurable baseline |
| 1 | `keep_alive` + prewarm both models | `chat.py`, `create_embeddings.py` | cold start removed |
| 2 | Strip repeated titles/XML from prompts | `chat.py` | ~6.5 s |
| 3 | Token budget + adaptive depth | `chat.py` | ~2.5-3.5 s |
| 4 | Precomputed paragraph embeddings | `create_embeddings.py`, `chat.py` | **~2-3 s** |
| 5 | Packed NumPy embeddings (optional) | `chat.py`, migration script | snappier `/tag` |
| 6 | MMR / per-video cap / dedupe | `chat.py` | ~1.5-2.5 s, better focus |
| 7 | Remove blocking Chinese translation | `chat.py` | Chinese path ≈ English |
| 8 | Answer cache | `chat.py` | repeats ≈ instant |
| 9 | Progressive streaming (optional) | `chat.py` | **<1 s perceived** |
| 10 | Regression + benchmark gate | `research/` | no quality regression |

### Implementation status (2026-10-27)

All steps in this plan are implemented in `chat.py` unless noted:

- **Step 0:** `answer()` and `stream_answer()` now print retrieval, context,
  prefill, model load, end-to-end TTFT, actual prompt tokens, cached prompt
  tokens, and estimated context per question.
- **Step 1:** `keep_alive: 30m` is sent to every embed/generate request, and a
  daemon `prewarm()` thread loads both models at startup.
- **Step 2:** `build_context()` emits each video title once and bare paragraphs;
  the repeated XML/title boilerplate is gone.
- **Step 3:** auto retrieval (the new default) uses a 4,096 estimated-token
  budget (6,144 for list/detail questions), a score elbow/floor, and
  `--max-context-tokens`. `/depth <n>` remains an explicit hard cap.
- **Step 4:** `create_embeddings.py` writes `.paragraph.embedding` files and
  `research/migrate_paragraph_embeddings.py` backfilled the existing 5,231
  trunks. Query-time paragraph re-embedding has been removed.
- **Step 5:** `research/pack_embeddings.py` builds `.chat/packs/<tag>.npy`
  (float32) plus metadata; `chat.py` uses the mmap automatically when fresh and
  falls back to JSON when stale. `--no-packed-embeddings` forces the fallback.
- **Step 6:** auto retrieval removes near-duplicates (> 0.95 cosine), caps one
  video at 5 selected chunks, stops at the score floor/elbow, and applies the
  token budget.
- **Step 7:** Simplified Chinese questions are embedded directly and answered
  in Simplified Chinese via a prompt instruction; no blocking translation calls
  remain on the path.
- **Step 8:** a 32-entry LRU answer cache is keyed by tag, transcript, depth,
  budget, language, progressive mode, and normalized question, and is cleared by
  `/tag`, `/refresh`, `/transcript`, `/learn`, and `/depth`.
- **Step 9:** optional `--progressive` streams a 10-chunk first pass and then an
  expansion pass over the remaining selected chunks. The default remains the
  single coherent call; the expansion is sequential rather than overlapped.
- **Step 10:** `research/benchmark_ttft.py` runs the regression question set and
  reports medians for TTFT, prefill, retrieval, prompt tokens, context tokens,
  and total time, while exposing Ollama's cached-prefix count.

Measured after implementation on this machine (2,916 Japan chunks, warm models,
packed embeddings, auto budget): packed load ~0.09 s, retrieval ~0.05-0.08 s,
prompt ~650-900 tokens, and end-to-end TTFT ~0.6-0.9 s for the sampled
questions. These are far below the ~9.5 s baseline; re-run
`research/benchmark_ttft.py` for current medians.

### Step 0: Instrument before changing anything

Goal: make every later change measurable and catch regressions immediately.

1. In `answer()` (`chat.py`), record monotonic timestamps around:
   - question embedding (`create_embedding`),
   - retrieval (`top_chunks`),
   - context assembly (`build_context`),
   - prompt construction (`PROMPT.format`).
2. In `stream_answer()` (`chat.py`), capture:
   - time of the first non-empty `token` in the stream (request TTFT),
   - the final event's `load_duration`, `prompt_eval_count`,
     `prompt_eval_cached_count`, `prompt_eval_duration`, `eval_count`, and
     `eval_duration` when `done` is true.
3. Print one compact line per question, for example:

   ```text
   [retrieval 0.02 s | prefill 2.10 s | load 0.00 s | TTFT 2.20 s |
    prompt_tokens 3210 (cached 0) | context 3980 estimated]
   ```

4. Keep the existing context-size/window line; it is still useful to the user.
5. Optional: add `research/benchmark_ttft.py` that sends the same list of
   regression questions to a helper function and reports median TTFT, prompt
   tokens, and retrieval time. Do not benchmark by re-asking the identical
   question: Ollama's prefix cache will report 46-73 ms instead of real prefill.

Acceptance gate: the instrumentation reproduces the depth-100 row of section
4.2 within normal variance (retrieval ~1.8 s, prefill ~7.7 s, TTFT ~9.5 s).

### Step 1: Model lifecycle (`keep_alive` + prewarm)

Goal: remove the ~1-2 s cold-start penalty from the first question after
startup or after 5 minutes of idle.

1. Add `"keep_alive": "30m"` to every Ollama payload:
   - `create_embeddings()` -> `/api/embed` in `chat.py`,
   - `generate_text()` -> `/api/generate` in `chat.py`,
   - `stream_answer()` -> `/api/generate` in `chat.py`,
   - `create_embedding()` -> `/api/embed` in `create_embeddings.py`.
2. Add `prewarm()` in `chat.py` and start it in a daemon thread at the top of
   `main()`, before the first `session.prompt()`:
   - one `/api/embed` request with `input: ["warmup"]`,
   - one `/api/generate` request with an empty prompt,
   - both with `keep_alive: "30m"` and short connect timeouts.
3. Do not block startup on the prewarm; the user may type while it runs.
4. Confirm `OLLAMA_MAX_LOADED_MODELS >= 2` (default 3 on this machine) so the
   chat model does not evict the embedding model, and keep
   `OLLAMA_NUM_PARALLEL=1` for a single user.
5. If an embedding request runs while the chat model is still loading, it may
   wait; that is acceptable because it happens in the background.

Acceptance gate: first question after a fresh start reports `load_duration`
< 50 ms, and `ollama ps` shows both models resident at 100% GPU.

### Step 2: Prompt hygiene

Goal: remove repeated titles and XML wrappers from the context.

1. Rewrite `build_context()` (`chat.py`) to emit paragraphs, not raw trunks:
   - reuse `PARAGRAPH_PATTERN` / `paragraph_text()` to extract the body,
   - read each video's title once from
     `.chat/youtube_transcripts/<video_id>.title`,
   - emit the title once before that video's first paragraph:

     ```text
     [Video: <title>]
     <paragraph>

     <paragraph>

     [Video: <other title>]
     <paragraph>
     ```

   - if a title file is missing, omit the marker rather than inventing one.
2. Keep the `<context>...</context>` wrapper in `PROMPT`; it is stable and
   cheap, and the wrapper is already part of the cached prefix.
3. Do not move the question in front of the context. The question must remain
   last so the stable prefix stays cacheable.
4. `/sources` is unaffected: it uses `chunk.label` and does not read the
   prompt.

Acceptance gate: `estimate_tokens(context)` drops 30-40% for the same chunk
set; the regression answers (step 10) are substantively unchanged.

### Step 3: Token budget and adaptive depth

Goal: stop treating 100 chunks as a target; cap context by tokens.

1. Add constants/flags:
   - `MAX_CONTEXT_TOKENS = 4096` (estimated tokens),
   - `LIST_CONTEXT_TOKENS = 6144` for list/all/compare/every/details queries,
   - CLI `--max-context-tokens <n>` (0 disables the budget),
   - `/depth auto` as the new default; `/depth <n>` remains an explicit
     hard cap for users who want it.
2. In `top_chunks()` / `answer()`, walk the ranked results in score order and
   keep a chunk only while the accumulated estimated token count plus the
   next paragraph stays under the budget.
   - Always keep at least one chunk; do not return an empty context.
   - Keep at least `FALLBACK_TOP_N` chunks when they clear the similarity
     threshold.
3. Add deterministic score-elbow stopping (no extra LLM call):
   - continue while the score clears `MIN_SIMILARITY`, and
   - stop when the drop from the previous score exceeds ~0.08-0.10, or the
     budget is exhausted.
4. Raise the budget only for questions containing list/all/compare/every/
   details; this is a cheap string check, not a classifier model call.
5. Leave `--depth 100` available as a manual override; do not silently
   truncate an explicitly requested depth, but still report the budget.
6. Verify `ANSWER_NUM_CTX` remains well above the largest prompt (currently
   32,768); never lower `num_ctx` below the prompt size.

Acceptance gate: typical questions use 3-4k actual prompt tokens; warm
end-to-end TTFT is 2.8-3.5 s; list questions still enumerate all expected
items.

### Step 4: Precompute paragraph embeddings (largest retrieval win)

Goal: delete the per-query `rerank_on_paragraphs()` network call.

1. In `create_embeddings.py`, after writing the existing trunk embedding:
   - extract the paragraph with the same regex used by `paragraph_text()`,
   - embed the paragraph text,
   - write `<video_id>.<index>.paragraph.embedding` next to
     `<video_id>.<index>.embedding`.
   - Reuse one batch request per trunk or batch several paragraphs if that is
     simpler; correctness matters more than learn-time speed.
2. Add a one-time migration script (for example
   `research/migrate_paragraph_embeddings.py`) that walks all trunk files and
   writes the missing paragraph embeddings. Do not re-embed at query time.
3. In `chat.py`:
   - extend `EMBEDDING_FILE` to recognize `.paragraph.embedding`,
   - add `paragraph_embedding: list[float]` to `Chunk`,
   - in `load_embeddings()`, pair each trunk with its paragraph embedding and
     fall back to `embedding` when the file is absent (old data),
   - make `top_chunks()` score with `chunk.paragraph_embedding` directly,
   - remove the `rerank_on_paragraphs()` call; keep the function only as a
     temporary fallback or delete it once all data is migrated.
4. Keep `MIN_SIMILARITY` and the fallback-top-N behavior unchanged so answer
   selection semantics stay comparable.
5. Re-run the regression questions before/after; the paragraph-only score
   should reduce the "one video dominates" problem described in section 4.5.

Acceptance gate: warm `top_chunks()` < 50 ms (target ~20 ms including the
14 ms question embedding); retrieval no longer appears in the profile.

### Step 5: Packed embeddings and NumPy (optional, do after step 4)

Goal: make startup, `/tag`, `/refresh`, and `/transcript` fast and reduce RSS.

1. Build one `<tag>.npy` float32 matrix plus a JSON metadata file mapping row
   index -> `(video_id, trunk_index, paragraph_embedding row)`.
2. Load with `mmap_mode="r"` and score with a matrix-vector product.
3. Keep the JSON embedding loader as a fallback for tags without a pack.
4. Rebuild the pack whenever `load_embeddings()` would have changed (after
   `/learn`, `/refresh`, or a tag edit).

Acceptance gate: startup for 3k chunks < 0.1 s and ranking < 1 ms; memory
returns near 9-15 MB instead of ~134 MB.

### Step 6: Context quality while reducing size

Goal: make the reduced context more informative, not just smaller.

1. Add a per-video cap (start with 4-6 chunks per video in a 30-chunk set).
2. Drop near-duplicates whose paragraph cosine with an already-selected chunk
   exceeds ~0.95.
3. Optionally add MMR with a small diversity penalty (0.1-0.3).
4. Add the score-floor rule: stop when the next score is below
   `max(MIN_SIMILARITY, top_score - 0.15)`.
5. Optionally add sentence-level extractive compression: keep sentences
   containing question entities/terms plus one neighbour.
6. Measure answer focus on the regression set, not only token count.

Acceptance gate: 30-50% fewer context tokens for comparable coverage, and no
regression on the list/visa/market questions.

### Step 7: Fix the Simplified Chinese critical path

Goal: remove the ~1-1.6 s blocking translation overhead.

1. First try embedding the Chinese question directly with
   `embeddinggemma-2:570m` and skipping `translate_to_english()` for retrieval.
   Compare retrieval results on Chinese paraphrases of the regression set.
2. If retrieval quality is acceptable, keep the original question for display
   only; remove the pre-retrieval translation call.
3. Ask the chat model to answer in Simplified Chinese directly by adding one
   instruction to the prompt, then stop post-translating every segment.
4. If `qwen3:1.7b` mixes languages, fall back to one of:
   - overlap translation with generation using an ordered worker queue, or
   - a faster translation model for short segments.
5. Keep `contains_simplified_chinese()` for routing; do not remove the
   translation prompt templates until the new path is proven.

Acceptance gate: Chinese end-to-end TTFT is within ~0.3 s of the English path,
the first Chinese output is not delayed by a translation call, and answers do
not mix languages.

### Step 8: Answer cache

Goal: repeated and near-repeated questions become instant.

1. Add an in-process LRU keyed by
   `(tag, transcript, depth, max_context_tokens, normalized_question)`.
2. Store the answer text, sources, and context estimate.
3. Invalidate on `/tag`, `/refresh`, `/transcript`, `/learn`, `/depth`, and
   `--max-context-tokens` changes.
4. Keep it bounded (for example 32 entries) so memory stays predictable.

Acceptance gate: asking the same question twice returns the second answer in
milliseconds; invalidating the cache then re-asking produces a fresh answer.

### Step 9: Progressive streaming (optional)

Goal: perceived TTFT under 1 s for broad questions.

1. Reuse the design in `research/low_context_streaming_rag_architecture.md`
   and the existing `chat_gradual_context.py` / `chat_lowcontext.py`.
2. First pass: top 8-12 chunks (~1k tokens), stream the opening paragraph.
3. Expansion pass: issue the second request with the remaining selected
   context while the first pass streams; stitch with an "already covered"
   ledger to avoid repetition.
4. Keep the single-call path as the default fallback for users who prefer one
   coherent answer.

Acceptance gate: first visible token < 1 s on a fresh broad question, with no
obvious duplication or tone shift in the final answer.

### Step 10: Regression and benchmark gate

Run the same question set before and after each step:

- "list me all levels of wagyu in details"
- "what time does nijo market open"
- "where is nijo market"
- "i want to get 90-day long-stay visa, what are the requirements"
- "show me list of airports"
- "show me a list of small towns"
- "i want to buy electronics in japan, give me a recommendation in 600 words"
- "can i do left turn on red light"
- the Chinese equivalent of one of the above

For each, record: correctness, focus, coverage, estimated/actual prompt
tokens, retrieval time, request TTFT, and end-to-end TTFT. Use three distinct
questions/contexts per configuration and report the median; do not count a
cache hit as a prefill measurement.

Definition of done for this plan:

- Typical warm question: **<= 3.5 s end-to-end TTFT** at a 3-4k-token context.
- Repeated question: effectively instant (cache hit).
- Chinese question: no more than ~0.3 s slower than English.
- No regression on the question set above.
- Instrumentation remains in the chat output so future changes stay measurable.

### Rollback and flags

- Each step should be independently revertible; prefer a config flag for the
  larger changes (`--max-context-tokens 0` disables the budget,
  `--depth 100` restores the old selection size).
- Keep the old `rerank_on_paragraphs()` path until paragraph embeddings have
  been migrated for every tag; the fallback must not silently return empty
  context.
- If a change hurts answer quality, revert that step before attempting the next
  one; do not stack unverified optimizations.

## 2. Environment and method

Measurements were taken on:

| Item | Value |
|---|---|
| Machine | Apple M3 Pro, 12 CPUs, 36 GB unified memory |
| OS | macOS 26.7.1 |
| Ollama | 0.40.6 |
| Chat model | `qwen3:1.7b` (Q4_K_M, 2.0B, 40,960 max context) |
| Embedding model | `embeddinggemma-2:570m` (768-dimensional output) |
| Tag | `japan` |
| Loaded chunks | 2,916 |
| Total embedding files on disk | 5,231 |

TTFT is measured as:

```
press Enter -> create question embedding -> retrieve chunks -> build prompt
             -> POST /api/generate (stream) -> first non-empty response token
```

The report separates:

- **retrieval latency**: question embedding + ranking + context file reads;
- **request TTFT**: from the HTTP POST to the first non-empty token, which
  includes model load and prompt prefill;
- **end-to-end TTFT**: retrieval + request TTFT;
- **total answer time**: until the stream ends.

Prompt sizes and prefill times come from Ollama's own final stream event:
`prompt_eval_count`, `prompt_eval_duration`, `load_duration`, `eval_count`, and
`eval_duration`. `prompt_eval_duration` times only uncached prompt tokens; this
distinction matters for benchmarking (see section 9).

## 3. Current code path

The relevant path in `chat.py` is:

| Step | Location |
|---|---|
| Startup tag load and embedding load | `load_embeddings()`, `chat.py:288` |
| Question embedding | `create_embedding()`, `chat.py:751` |
| Trunk cosine shortlist | `top_chunks()`, `chat.py:847` |
| Paragraph re-ranking (network embedding call) | `rerank_on_paragraphs()`, `chat.py:796` |
| Context text assembly | `build_context()`, `chat.py:904` |
| Prompt construction | `PROMPT`, `chat.py:150` |
| Answer stream | `stream_answer()`, `chat.py:1045` |
| Per-question orchestration | `answer()`, `chat.py:1591` |
| Chat loop and startup | `main()`, `chat.py:1657` |

Per question, the current implementation does the following synchronously:

1. `POST /api/embed` to embed the question (`embeddinggemma-2:570m`).
2. Score all loaded trunk embeddings in Python.
3. Take the top 100 candidates.
4. **Read each candidate's trunk file, extract `<paragraph>`, and send all 100
   paragraph texts back to Ollama in one `/api/embed` request.**
5. Re-score the 100 candidates on those new embeddings.
6. Read the selected trunk files again and concatenate their full XML.
7. `POST /api/generate` with a prompt whose context is placed before the
   question.

Step 4 is the main avoidable retrieval cost. Step 7 is the main TTFT cost.

## 4. Measured latency breakdown

### 4.1 Startup

Loading 2,916 embeddings (JSON files, normalized to Python floats) takes
**0.39 s** and peaks at about **134 MB RSS**. This is paid once at startup and
does not affect per-question TTFT, but it is easy to reduce with a packed array.

### 4.2 Per-question measurements

All runs below used a warm chat model and warm embedding model, and distinct
questions/contexts so that Ollama's prompt cache did not mask prefill work.

| Depth | Chunks | Estimated context tokens | Actual prompt tokens | Retrieval | Prompt prefill | Request TTFT | End-to-end TTFT |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 100 | 100 | 11,023 | 8,879 | 1.80 s | 7.65 s | 7.69 s | **9.49 s** |
| 30 | 30 | 3,498 | 2,827 | 1.94 s | 1.84 s | 2.05 s | **3.99 s** |
| 10 | 10 | 1,168 | 995 | 1.75 s | 0.63 s | 0.67 s | **2.42 s** |

Cold first question at depth 100: retrieval 1.76 s + request TTFT 9.62 s
(load 1.03 s, prefill 8.54 s at 9,715 prompt tokens) = **11.4 s**.

Repeating the exact same prompt after a warm run: request TTFT **46-73 ms**,
because Ollama reuses the cached prompt prefix. This is useful for repeated
questions but is not representative of a fresh question.

### 4.3 Prefill throughput

The measured prefill rate is roughly **1,100-1,600 prompt tokens/second** for
`qwen3:1.7b` on this machine:

- 8,879 tokens / 7.653 s = 1,160 tok/s
- 2,827 tokens / 1.842 s = 1,535 tok/s
- 995 tokens / 0.632 s = 1,574 tok/s

A useful rule of thumb: **each 1,000 prompt tokens costs 0.65-0.9 s of TTFT**.
This makes context size the dominant lever. Reducing the prompt from 8,900 to
3,000 tokens removes about 5.5 seconds of prefill.

### 4.4 Retrieval cost is dominated by one embedding request

`top_chunks()` always uses `RERANK_CANDIDATES = 100`; `/depth` changes only how
many of the re-ranked results are returned, not how many are re-embedded. That
is why retrieval took 1.75-1.94 s at every depth in the table above.

For comparison, on the same loaded chunks:

- Pure Python dot products over 2,916 x 768 embeddings: **0.19 s**.
- NumPy dot product (after building a float32 matrix): **0.05 ms** per query.
- Question embedding, warm: **14 ms**; cold: **0.96 s**.
- `top_chunks()` with the 100-paragraph re-embed, warm: **1.75-1.82 s**.

In other words, nearly all of the retrieval latency is the repeated embedding
network call, not the vector math.

### 4.5 Context redundancy at depth 100

For the question "list me all levels of wagyu in details":

| Metric | Value |
|---|---|
| Selected chunks | 100 |
| Distinct videos | 16 |
| Chunks from the largest single video | 28 |
| Tokens from that video | 3,529 / 10,982 (32%) |
| Mean pairwise cosine among selected trunk embeddings | 0.785 |
| Maximum pairwise cosine | 0.955 |
| Chunks with similarity > 0.75 | 15 |
| Chunks with similarity > 0.70 | 53 |
| Chunks with similarity > 0.60 | 100 |

The top-100 set contains many near-duplicates and one video's perspective
dominates. Adding chunks 16-100 buys little new information while adding
roughly 6,000 prompt tokens and ~5 s of prefill.

### 4.6 Repeated titles are a large fixed overhead

Every trunk is formatted as:

```xml
<article><title>Same video title</title><paragraph>...</paragraph></article>
```

The title is repeated in every chunk from that video. Across the selected 100
chunks, titles alone accounted for about **2,969 estimated tokens**, roughly
27% of the estimated context. Replacing each title with a short fixed marker
reduced the estimated context from 11,023 to 7,700 tokens (-30%) and prefill
from 7.65 s to 4.71 s (-38%) for a comparable query. Removing the XML wrappers
as well would save more.

### 4.7 Model cold start

| Event | Cold | Warm |
|---|---:|---:|
| Chat model load (`qwen3:1.7b`) | ~1.03 s | 3-5 ms |
| Embedding model load (`embeddinggemma-2:570m`) | ~0.96 s | 3-5 ms |
| First question embedding | 0.96 s | 14 ms |

Ollama's default keep-alive is 5 minutes (official FAQ). After an idle period,
the next question pays these reload costs again. Preloading and a longer
`keep_alive` remove them from the first real question.

### 4.8 Simplified Chinese path

The Chinese path adds two blocking model calls:

| Step | Warm latency |
|---|---:|
| `translate_to_english()` before retrieval | ~1.04 s |
| First `translate_to_chinese()` sentence group before any Chinese output | ~0.55 s |

So a Chinese question can pay **~1.6 s more** than the equivalent English
question, on top of the same retrieval and prefill costs. If the answer is
translated sentence group by sentence group, later translations overlap with
generation, but the first one is on the critical path.

## 5. Root causes, ranked by impact

1. **Too much context / prompt prefill (7.7 s at depth 100).**
   `DEFAULT_DEPTH = 100` is effectively a 9-11k-token prompt. Prefill scales
   linearly with prompt tokens at ~1,100-1,600 tok/s.
2. **Per-query paragraph re-embedding (1.8 s).**
   `rerank_on_paragraphs()` sends 100 paragraphs to `/api/embed` on every
   question. The result is deterministic per chunk and should be precomputed.
3. **Repeated title/XML boilerplate (up to 30-40% of context tokens).**
   It is pure overhead in the prompt and is repeated per chunk.
4. **Cold model loads (0-2 s).** Default 5-minute keep-alive plus no preload.
5. **Blocking Chinese translation (1-1.6 s).** Added before retrieval and before
   the first translated segment.
6. **Redundant chunk selection (context quality and size).** No deduplication,
   diversity control, or per-video cap.
7. **Startup embedding load (0.39 s).** Not per-question, but easy to reduce and
   it also consumes 134 MB in Python objects.

## 6. Optimization options

### P0: quick wins with low risk

#### 6.1 Precompute paragraph embeddings

**Problem.** `rerank_on_paragraphs()` recomputes the same 100 paragraph
embeddings for every question.

**Fix.** Add a paragraph-only embedding per trunk at learn time, and use it for
retrieval. This also removes the title bias that motivated the re-ranking step
in the first place.

Options, from cleanest to least invasive:

- **Option A (recommended):** change `create_embeddings.py` to write an
  additional `<video>.<index>.paragraph.embedding` containing the embedding of
  `paragraph_text()`. At load time, prefer the paragraph embedding and fall
  back to the trunk embedding for old data. A one-time migration script can
  backfill existing files.
- **Option B:** change the existing trunk embedding to paragraph-only. This is
  simplest, but changes retrieval semantics for all existing data and requires
  re-embedding.
- **Option C:** cache paragraph embeddings in a per-tag `.npy` + metadata file
  the first time they are computed. Less clean than A, but avoids touching the
  learn pipeline.

**Expected effect:** retrieval 1.75-1.94 s -> ~20 ms (14 ms question embedding
+ a few ms ranking), assuming the embedding model is warm.

**Implementation sketch:**

```python
# create_embeddings.py (after computing the normal trunk embedding)
paragraph = extract_paragraph(trunk_text)
para_embedding = embed(paragraph)
write_json(f"{video_id}.{index}.paragraph.embedding", para_embedding)

# chat.py
@dataclass
class Chunk:
    video_id: str
    index: int
    embedding: list[float]          # existing trunk embedding
    paragraph_embedding: list[float]  # new; fall back to embedding
    trunk_path: Path

# top_chunks(): score with chunk.paragraph_embedding, no rerank call
```

For 2,916 chunks at 768 dimensions, the paragraph matrix is only 9 MB as
float32 (15 MB for all 5,231 chunks). Precomputing it once is cheap compared
with paying 1.8 s on every question.

**Quality check:** compare answers on the existing regression questions
(wagyu, Nijo Market, 90-day visa, Suica) with and without titles in the
retrieval embedding. If title context matters for broad queries, keep a
title-only embedding as a secondary signal rather than embedding the combined
title+paragraph.

#### 6.2 Cap context by token budget

**Problem.** `/depth 100` treats 100 chunks as a target rather than a maximum.

**Fix.** Introduce a context budget (for example 4,000 estimated tokens) and
add chunks in score order until the budget is reached. Keep `/depth` as an
explicit override for users who intentionally want a large context.

Suggested behavior:

```text
depth == "auto":   keep chunks while score is above threshold and budget remains
/depth <n>:        hard maximum n chunks, still subject to the budget?
--max-context-tokens <n>: override the budget (default 4096)
```

The existing `estimate_tokens()` is a rough over-estimate (11,023 estimated vs
8,879 actual at depth 100), which is acceptable for a conservative budget. A
4,000-token estimated budget corresponds to roughly 3.2-3.5k actual prompt
tokens.

**Expected effect:** at ~3k actual prompt tokens, prefill is ~2 s; at ~4k it is
~2.6-3.5 s. Combined with 6.1, end-to-end TTFT becomes ~2-3.5 s.

**Quality check:** list-style questions may benefit from more context. Keep a
larger budget for questions containing "list", "all", "compare", "every", or
"details", or use the two-stage design in section 6.7. Do not use another LLM
call just to classify the question; it would add latency.

#### 6.3 Strip repeated titles and wrappers

**Problem.** Each chunk repeats a long title and four XML tags.

**Fix.** Put each paragraph in the prompt once, with a short separator, and if
titles are useful, include each video's title once before its first chunk:

```text
[Video: <title>]
<paragraph 1>

<paragraph 2>

[Video: <other title>]
<paragraph 3>
```

Or, if titles are not needed for answering, use bare paragraphs separated by
`\n\n---\n\n`.

**Expected effect:** measured -30% estimated tokens and -38% prefill on a
comparable query. Removing wrappers as well should save more. Source labels
for `/sources` remain in the `Chunk` objects and do not need to be in the
prompt.

#### 6.4 Preload models and keep them alive

**Problem.** The first question after startup or after 5 minutes of idle pays
model load time.

**Fix.**

- Add `"keep_alive": "30m"` (or `-1`) to every `/api/embed` and
  `/api/generate` request.
- Preload both models at startup with an empty/one-token request, in a
  background thread, so the user can start typing while they warm up.
- Ensure `OLLAMA_MAX_LOADED_MODELS >= 2` so loading the chat model does not
  evict the embedding model. The default is 3 on CPU/one GPU, which is enough.
- Use `OLLAMA_NUM_PARALLEL=1` for single-user lowest latency (the default).

```python
import threading, requests

def prewarm() -> None:
    try:
        requests.post(
            f"{OLLAMA_HOST}/api/embed",
            json={"model": EMBED_MODEL, "input": ["warmup"],
                  "keep_alive": "30m"},
            timeout=(10, 120),
        )
        requests.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": CHAT_MODEL, "prompt": "", "keep_alive": "30m"},
            timeout=(10, 120),
        )
    except requests.RequestException:
        pass

threading.Thread(target=prewarm, daemon=True).start()
```

Ollama's official FAQ documents both preloading with an empty request and the
`keep_alive` semantics.

**Expected effect:** removes ~1.0 s (chat) and ~1.0 s (embedding) from the first
question after startup or idle. It does not reduce steady-state TTFT.

#### 6.5 Fix the Chinese critical path

**Problem.** Question translation blocks retrieval, and the first answer
segment translation blocks the first Chinese output.

**Options:**

1. **Embed the Chinese question directly.** `embeddinggemma-2` is a
   multilingual embedding model. If retrieval quality on Chinese queries is
   acceptable, skip `translate_to_english()` entirely for retrieval and keep
   translation only for display. This removes ~1.04 s.
2. **Ask the chat model to answer in Simplified Chinese directly.** Add an
   instruction such as "Answer in Simplified Chinese" to the prompt and stop
   post-translating sentence groups. This removes the per-segment translation
   call (~0.55 s before first output and additional calls during generation).
   Test quality on `qwen3:1.7b`; small models sometimes mix languages.
3. **Overlap translation with generation.** Keep an ordered worker queue: as
   English segments arrive, translate them concurrently and emit them in
   order. This does not remove the first-segment latency, but hides later
   translation behind generation.
4. **Use a faster translation model** (for example `gemma3:270m`) for the
   question and short segments. Lower quality risk; measure.

**Expected effect:** option 1 + 2 removes ~1.6 s from the Chinese path; option 1
alone removes ~1.0 s.

#### 6.6 Add an answer/retrieval cache

**Problem.** Repeated or near-repeated questions redo retrieval, prefill, and
generation.

**Fix.** An in-process LRU keyed by `(tag, transcript, depth, normalized
question)` that stores the answer text, sources, and context. Invalidate it on
`/tag`, `/refresh`, `/transcript`, `/learn`, and `/depth`.

**Expected effect:** repeated questions become effectively instant. Ollama's
prompt cache already gives ~46-73 ms request TTFT for an identical prompt, but
an application cache also avoids generation and retrieval. This is a small win
for unique questions and a large win for repeated ones.

### P1: medium-effort improvements

#### 6.7 Deduplicate and diversify the retrieved set

**Problem.** Top-100 is dominated by near-duplicates and one video.

**Fixes:**

- **MMR (maximal marginal relevance):** greedily select chunks that are
  relevant to the question but dissimilar to already-selected chunks. A
  similarity penalty of 0.1-0.3 on the cosine scale is a reasonable start.
- **Per-video cap:** for example, at most 4-6 chunks per video in a 30-chunk
  context. This prevents one transcript from consuming a third of the prompt.
- **Near-duplicate removal:** drop a candidate whose paragraph embedding is
  above ~0.95 cosine with an already-selected chunk.
- **Score cutoff:** stop when the next chunk is below
  `max(MIN_SIMILARITY, top_score - 0.15)`.

**Expected effect:** 30-50% fewer tokens for similar coverage, and likely better
answer focus. This also makes the remaining budget more informative.

#### 6.8 Query-aware extractive compression

After selecting chunks, keep only sentences that contain query entities/terms
plus one neighboring sentence for context. This is cheap CPU work (milliseconds)
and can remove boilerplate, introductions, and off-topic sentences.

**Expected effect:** another 20-40% token reduction, depending on the chunk
style. Risk: dropping a sentence that matters for synthesis. Measure on the
regression questions before enabling by default.

#### 6.9 Packed embeddings and NumPy

Store all embeddings for a tag in a single `.npy` matrix plus a JSON metadata
file. Load with `mmap_mode="r"` and score with a matrix-vector product.

Measured effects:

- Startup: 0.39 s -> 0.035 s with mmap (0.001 s for a full load of the 9 MB
  matrix).
- Ranking: 0.19 s pure Python -> 0.05 ms NumPy.
- Memory: 9 MB float32 vs ~134 MB of Python float objects.

This is not the main TTFT lever, but it makes `/tag`, `/refresh`, `/transcript`,
and startup noticeably snappier and leaves more memory for the models.

#### 6.10 Adaptive depth using the score distribution

Instead of a fixed depth, use the score curve:

- Keep the top chunk.
- Continue while `score[i] > 0.75`, or while the drop from the previous chunk
  is small, or while the token budget allows.
- Stop at a strong elbow (for example, a drop greater than 0.08-0.10 between
  consecutive scores).

This automatically uses ~10-15 chunks for the wagyu query (15 chunks above
0.75) and more for broad list questions. No extra model call is needed.

#### 6.11 Prompt layout and prompt caching

Ollama caches prompt prefixes. The current layout, context first and question
last, is already the right direction: stable material should come first and
the volatile question last. Two additional points:

- Adding a fixed instruction/system preamble before the context gives the cache
  a small stable prefix, but most of the prompt is still volatile.
- If retrieved context sets overlap heavily between consecutive questions,
  canonical ordering by `(video_id, index)` can increase prefix reuse, but it
  may hurt relevance ordering. Measure; do not assume.
- Do not move the question to the front of the prompt. That would invalidate
  the cache at the first volatile token.

### P2: architectural options

#### 6.12 Two-stage / progressive streaming

Instead of one 100-chunk prompt, make two calls:

1. **Fast pass:** top 8-12 chunks (~1k tokens), stream the opening of the
   answer. Request TTFT is ~0.6-0.9 s on this machine.
2. **Expansion pass:** while the first pass streams, issue a second request
   with the full selected context (or the remaining chunks) and append its
   output, or use it to refine the answer. The second prefill is hidden behind
   the first answer's generation.

This is the approach explored in
`research/low_context_streaming_rag_architecture.md` and in
`chat_gradual_context.py` / `chat_lowcontext.py`. It can bring **perceived**
TTFT under 1 s without changing the model.

Costs: more complex prompt/stitching logic, possible repetition or tone shifts,
and a second model call. Use a rolling summary or "already covered" ledger to
keep the answer coherent. Keep the single-call path for users who prefer it.

#### 6.13 Chunk-level KV cache reuse

Research systems such as TurboRAG and Cache-Craft precompute per-chunk KV
caches and stitch them at query time, reporting large TTFT reductions (TurboRAG
reports 8.6x average / up to 9.4x on multi-document QA; Cache-Craft reports
2-3x). They require engine support for cache stitching (vLLM/SGLang/llama.cpp
internals) and, in TurboRAG's case, fine-tuning for accuracy. Ollama's
`/api/generate` does not expose KV-cache stitching.

This is not a practical next step for `chat.py`. Revisit only if the project
moves to a custom serving engine and can accept the accuracy/complexity risk.

#### 6.14 Engine and model choice

- `qwen3:1.7b` is already small. Prefill cost scales with model size, so a
  smaller model (for example `gemma3:270m`) would be faster but is likely to
  reduce answer quality. Use it only for routing or translation, not as a
  blanket replacement.
- On Apple Silicon, verify the backend with `ollama ps` (should be 100% GPU).
  Partial CPU offload can make prompt evaluation an order of magnitude slower
  (see Ollama issue #12037). This is not a concern for a 2B model on 36 GB, but
  check after changing models or context size.
- Flash Attention and quantized KV cache (`OLLAMA_KV_CACHE_TYPE=q8_0`) mainly
  reduce memory and can improve decode speed; they are not guaranteed to
  reduce prefill. Enable them for headroom, but measure TTFT rather than
  assuming a gain.

## 7. Recommended implementation plan

### Phase 0: instrument first (half day)

Add per-question timing and Ollama statistics to the context report:

```text
[retrieval: 0.02 s | prompt: 2.1 s | load: 0.00 s | TTFT: 2.2 s |
 prompt_tokens: 3210 (cached: 0) | context: 3980 estimated]
```

Log `load_duration`, `prompt_eval_count`, `prompt_eval_cached_count`, and
`prompt_eval_duration` from the final stream event. This makes every later
change measurable and prevents regressions.

### Phase 1: lifecycle and prompt hygiene (0.5-1 day)

- Add `keep_alive` to all Ollama requests.
- Prewarm both models at startup in a background thread.
- Strip repeated titles/XML from the prompt, keeping a title once per video if
  useful.
- Add a context token budget (default 4,000 estimated tokens).
- Keep `/depth` as an explicit override.

Expected result at depth 100 with title stripping: ~6.5 s end-to-end; with the
budget, ~3-4 s.

### Phase 2: retrieval rebuild (1 day)

- Precompute paragraph embeddings at learn time; migrate existing data.
- Score against paragraph embeddings directly; delete the per-query
  `rerank_on_paragraphs()` network call.
- Optionally pack embeddings into a per-tag `.npy` matrix for fast startup and
  NumPy ranking.
- Add a small LRU answer cache.

Expected result: retrieval ~20 ms, end-to-end **~2-3 s** at a 3-4k-token
context.

### Phase 3: quality-preserving context reduction (1-2 days)

- Add MMR or a per-video cap and near-duplicate removal.
- Add the score-elbow stopping rule.
- Optionally add sentence-level extractive compression.
- Fix the Chinese path (direct multilingual embedding and/or direct Chinese
  answering).
- Re-run the regression questions (wagyu, Nijo Market, 90-day visa, Suica,
  airports, small towns) and compare answer quality, not only latency.

Expected result: ~1.5-2.5 s end-to-end with equal or better focus, and a
Chinese path without the extra ~1.6 s.

### Phase 4: optional progressive streaming (2-4 days)

Implement the two-stage fast-pass/expansion design for broad and list-style
questions, or promote `chat_gradual_context.py`'s approach. Target **perceived
TTFT under 1 s**. Keep the single-call path as a fallback.

## 8. Projected results

These are projections based on the measured prefill rate and retrieval
latencies, not yet-implemented measurements.

| Configuration | Retrieval | Prefill | Cold load | End-to-end TTFT |
|---|---:|---:|---:|---:|
| Current, depth 100, cold | 1.8 s | 7.7 s | 1.0 s | ~10.5 s |
| Current, depth 100, warm | 1.8 s | 7.7 s | 0 | ~9.5 s |
| + prewarm / keep_alive | 1.8 s | 7.7 s | 0 | ~9.5 s (cold fixed) |
| + strip titles | 1.8 s | 4.7 s | 0 | ~6.5 s |
| + paragraph embeddings | 0.02 s | 4.7 s | 0 | ~4.7 s |
| + 4k-token budget | 0.02 s | 2.8 s | 0 | ~2.8 s |
| + 3k-token budget | 0.02 s | 2.0 s | 0 | ~2.0 s |
| Top-10 / ~1k tokens | 0.02 s | 0.65 s | 0 | ~0.7 s |
| Two-stage first pass (~8 chunks) | 0.02 s | ~0.5 s | 0 | ~0.5-0.7 s perceived |

The top-10 row is a latency lower bound for the current single-call design; it
is not a recommendation for all questions because list and synthesis queries
need more evidence.

## 9. Benchmarking protocol and pitfalls

1. **Warm up both models before measuring.** The first request includes model
   loading and is not representative. Report cold and warm numbers separately.
2. **Use distinct questions/contexts, or inspect the cache.** Ollama caches
   prompt prefixes; a repeated prompt gave 46-73 ms TTFT instead of ~7.7 s.
   Use `prompt_eval_cached_count` from the final event to see how many prompt
   tokens were served from cache.
3. **Do not compute prefill rate as `prompt_eval_count / prompt_eval_duration`
   on a cache hit.** `prompt_eval_count` includes cached tokens, while
   `prompt_eval_duration` times only computed tokens. Subtract
   `prompt_eval_cached_count` first.
4. **Do not lower `num_ctx` below the prompt size.** In a test, a 9,715-token
   prompt with `num_ctx=8192` was truncated to 4,098 prompt tokens, silently
   removing context, and the model reloaded (`load_duration` 553 ms). Check
   `prompt_eval_count` against `num_ctx` on every run.
5. **Do not assume `num_batch` helps.** On this machine, `num_batch=1024`
   measured 8.556 s prefill versus 8.543 s for the default at 9,715 tokens:
   no meaningful gain. It can help on other hardware; measure it.
6. **Measure end-to-end, not just the HTTP request.** Retrieval happens before
   `stream_answer()` starts. Reporting only request TTFT understates the user's
   wait by ~1.8 s in the current implementation.
7. **Use the same context size and output length when comparing models or
   settings.** Prefill and decode costs both change with context and output
   length.
8. **Run each configuration at least three times and use the median.** Prompt
   cache state, thermal state, and background load can move results.
9. **Verify GPU placement.** Run `ollama ps` after a request. Partial CPU
   offload can make prefill dramatically slower, especially for larger models.
10. **Check the token estimate against Ollama's count.** `estimate_tokens()`
    overestimated by about 24% in the depth-100 test (11,023 estimated vs 8,879
    actual). Use estimates for budgets but report actuals.

## 10. References

Local project documents:

- `research/low_context_streaming_rag_architecture.md` - progressive/streaming
  RAG design, rolling state, and two-phase extraction.
- `chat_gradual_context.py`, `chat_lowcontext.py` - existing alternative
  pipelines in this repository.

Ollama documentation and measurements:

- Ollama FAQ: context length, preloading, `keep_alive`, Flash Attention, KV
  cache types, parallel requests, max loaded models.
  https://docs.ollama.com/faq
- Ollama Generate API: `keep_alive`, `prompt_eval_count`,
  `prompt_eval_cached_count`, `prompt_eval_duration`, `load_duration`.
  https://docs.ollama.com/api/generate
- Deniz Genc, "Reported Ollama prefill rate of 13,826 tok/s was really 43 due
  to cache math" (2026): explains why cached tokens must be subtracted before
  computing prefill throughput.
  https://deniz.in/reported-ollama-prefill-rate-of-13-826-tok-s-was-really-43-due-to-cache-math
- Ollama issue #12037, "Excessive time to first token ... prompt eval about 9x
  slower": partial offload can severely degrade prompt evaluation.
  https://github.com/ollama/ollama/issues/12037
- Ollama issue #18072, prompt cache size is currently hard-coded (8 GiB):
  useful context on prefix-cache behavior.
  https://github.com/ollama/ollama/issues/18072

RAG TTFT research:

- Lu et al., "TurboRAG: Accelerating Retrieval-Augmented Generation with
  Precomputed KV Caches for Chunked Text", EMNLP 2025. Reports 8.6x average
  TTFT reduction (up to 9.4x) by precomputing and stitching chunk KV caches.
  https://aclanthology.org/2025.emnlp-main.334.pdf
- Agarwal et al., "Cache-Craft: Managing Chunk-Caches for Efficient
  Retrieval-Augmented Generation", 2025. Reports 2-3x TTFT reduction with
  chunk-level cache reuse and selective recomputation.
  https://arxiv.org/abs/2502.15734
- Zhu et al., "Accelerating Inference of Retrieval-Augmented Generation via
  Sparse Context Selection" (Sparse RAG), 2024. Parallel context encoding and
  selective decoding.
  https://arxiv.org/html/2405.16178
- Verma, "Contextual Compression in Retrieval-Augmented Generation for Large
  Language Models: A Survey", 2024. Survey of extractive, abstractive, and
  prompt-compression approaches.
  https://arxiv.org/html/2409.13385
