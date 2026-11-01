# Architectural Research: Low-Context Iterative & Streaming RAG with Small Local LLMs

## 1. Problem Statement & Core Tension
When retrieving a large volume of chunks ($N = 100$) in a Retrieval-Augmented Generation (RAG) system, stuffing all chunks into a single prompt exceeds the context window or degrades the reasoning performance ("lost in the middle" phenomenon) of small local LLMs (e.g., Llama-3-8B, Mistral-7B, Phi-3). 

Processing chunks sequentially ($1 \to 100$) solves the memory bottleneck and allows immediate Time-To-First-Token (TTFT) streaming. However, it introduces two fundamental challenges:
1. **Narrative Discontinuity:** Independent processing causes repetitive introductions, jarring transitions, and redundant information across the 100 outputs.
2. **Global Aggregation Failure:** Queries requiring holistic synthesis (e.g., *"How many seafood markets are in Sapporo?"*) cannot be answered accurately on chunk 1 because the total count is only known after chunk 100 is processed.

---

## 2. Challenge 1: Maintaining Coherence Across Sequential Chunks

To prevent the output from reading like 100 isolated paragraphs, the LLM needs a lightweight representation of **"what has already been said"** without loading the full history of previous chunks.

### Solution A: Rolling State + Tail-Window Context (Recommended for Streaming)
Instead of passing all previous outputs, maintain a fixed-size sliding buffer in your prompt for chunk $i$:
1. **Entity/Topic Ledger (State):** A compact list of entities or sub-topics already covered (e.g., `Covered so far: [Nijo Market, Curb Market]`).
2. **Last $K$ Sentences (Tail Buffer):** The exact last 2–3 sentences generated from chunk $i-1$.

**Prompt Template for Chunk $i$:**
```text
System: You are continuing an ongoing response to the user's query: "{query}".
Do NOT write an introduction or conclusion. 
Do NOT repeat information about topics already covered: {covered_ledger}.
Seamlessly continue the text from the following trailing sentences of the previous section:
"{last_2_sentences}"

New Context to integrate (if relevant and novel):
{chunk_i}
```
* **Handling Redundancy:** If chunk $i$ only contains information already in `{covered_ledger}`, instruct the local LLM to output a special token (e.g., `[SKIP]`), which your backend intercepts and suppresses from the user stream.

### Solution B: Clustering & Micro-Batching Before Streaming
Processing 100 individual chunks sequentially requires 100 separate LLM calls, creating massive prompt-processing overhead. 
* **Optimization:** Group the 100 embeddings by cosine similarity or metadata into 10–15 thematic clusters (or micro-batches of 5–8 chunks that easily fit a 4k/8k context window).
* **Benefit:** The LLM synthesizes 5–8 related chunks at once, naturally eliminating local redundancy and reducing the number of transitions needed from 100 down to ~15.

---

## 3. Challenge 2: Answering Global Aggregation & Counting Queries

There is a fundamental information-theoretic constraint: **You cannot stream the final sum ("There are 14 markets...") on token 1 before reading chunks 1 through 100.** 

To solve *"How many seafood markets are in Sapporo?"* while keeping low context and immediate streaming, you must decouple **extraction/streaming** from **final synthesis**.

### Architecture 1: Stream-Extract-Then-Summarize (Single-Pass Streaming)
Instead of trying to state the total number in the first sentence, flip the output structure: **stream the discoveries live as a running log/narrative, and stream the final count at the very end.**

1. **Maintain a Deterministic Accumulator in Code:** Keep a Python/TypeScript `Set()` or dictionary on the backend (e.g., `found_markets = set()`).
2. **Structured Dual-Output per Chunk:** Prompt the small LLM to output a lightweight JSON or delimiter-separated block containing newly discovered entities, followed by the streamed prose.
   ```text
   [ENTITIES: Nijo Market, Sapporo Central Wholesale Market]
   Nijo Market is one of the oldest public markets in Sapporo, offering fresh crab and scallops...
   ```
3. **Backend Interception:**
   * Your code parses `[ENTITIES: ...]` in memory, normalizes the names, and adds them to `found_markets` (preventing double-counting if Chunk 4 and Chunk 42 both mention "Nijo Market").
   * The prose portion is immediately streamed to the user UI.
4. **Final Aggregation Step (After Chunk 100):**
   * Once all chunks are processed, your code inspects `len(found_markets)` and triggers a final concluding stream: *"In total, based on the reviewed documents, there are **X** distinct seafood markets in Sapporo: [List]."*

### Architecture 2: Fast Map-Reduce with Progressive UI (Two-Phase)
If the user *must* see the direct count at the top of the response:
1. **Map Phase (Fast Extraction):** Run a tiny, ultra-fast prompt over chunks $1 \to 100$ (or in micro-batches of 10) asking *only* for entity extraction:
   * *Prompt:* `Extract names of any seafood markets in Sapporo mentioned in this text. Output JSON array only: ["name1", "name2"] or [].`
   * Because the output is only a few tokens per chunk, a local LLM can scan 100 chunks in a few seconds.
   * **Stream Progress to UI:** Stream a live status counter to the user: *"Scanning documents... Found 4 unique markets so far (Nijo Market, Curb Market...)"*
2. **Deduplication (Code + LLM):** Deduplicate the extracted list in code, and run one tiny LLM call to merge aliases (e.g., *"Sapporo Jogai Market"* == *"Curb Market"*).
3. **Reduce Phase (Streamed Final Answer):** Feed the deduplicated state into a single final prompt to stream a cohesive, well-structured answer starting with the exact count.

---

## 4. Recommended Hybrid Pipeline (Query-Routed Architecture)

Not all queries require counting, and not all queries require 100 chunks of prose. Implement a lightweight **Query Router** as Step 0:

```
                       +-------------------+
                       |    User Query     |
                       +---------+---------+
                                 |
                       +---------v---------+
                       | Local LLM Router  |
                       | (Classify Intent) |
                       +----+---------+----+
                            |         |
         +------------------+         +------------------+
         | Descriptive / Broad                           | Aggregation / Count / List
         v                                               v
+-------------------------------+               +-------------------------------+
|  Rolling-Window Streamer      |               |  Stateful Map-Reduce          |
|  1. Micro-batch (5 chunks)    |               |  1. Stream live extraction    |
|  2. Track `covered_ledger`    |               |     to backend Set/Accumulator|
|  3. Pass last 2 sentences     |               |  2. Deduplicate aliases       |
|  4. Stream cohesive prose     |               |  3. Stream final count +      |
|     immediately               |               |     synthesized breakdown     |
+-------------------------------+               +-------------------------------+
```

---

## 5. Reference Implementation Blueprint (Python Pseudocode)

Below is how to implement the **Stateful Rolling-Window Streamer** that handles both narrative continuity and entity counting simultaneously:

```python
def stream_iterative_rag(query: str, chunks: list[str], batch_size: int = 5):
    covered_entities = set()
    trailing_context = ""
    
    # 1. Stream an immediate framing header
    yield f"Analyzing {len(chunks)} relevant sections for: **{query}**...\n\n"
    
    # 2. Process in micro-batches to reduce LLM call overhead & improve local coherence
    for i in range(0, len(chunks), batch_size):
        batch_text = "\n---\n".join(chunks[i:i + batch_size])
        
        prompt = f"""You are incrementally answering the query: "{query}"
Already covered entities/facts (DO NOT REPEAT THESE): {list(covered_entities)}
Previous paragraph ending (continue smoothly from this tone/flow): "{trailing_context}"

Task:
1. Identify any NEW entities/items that directly answer the query from the New Context.
2. If there is new information, write 1-2 cohesive paragraphs continuing the response. If nothing new is present, output ONLY the word SKIP.

Format your output strictly as:
NEW_ENTITIES: comma, separated, list (or NONE)
CONTENT: <your seamless continuation prose or SKIP>

New Context:
{batch_text}
"""
        response = local_llm_generate(prompt)
        
        # Parse out the state vs the streamable content
        entities_line, content = parse_response(response)
        
        if entities_line != "NONE":
            for entity in entities_line.split(","):
                covered_entities.add(entity.strip())
                
        if content.strip() != "SKIP":
            yield content + "\n\n"
            # Keep the last ~200 characters as the bridge for the next chunk
            trailing_context = content[-200:]

    # 3. Final Aggregation Summary (Solves the "How many..." problem)
    summary_prompt = f"""Query: "{query}"
Complete list of unique items found across all documents: {list(covered_entities)}
Task: Deduplicate any obvious aliases in the list above, state the final exact count, and provide a brief concluding summary."""
    
    yield "### Summary & Total Count\n"
    for token in local_llm_stream(summary_prompt):
        yield token
```

---

## 6. Key Takeaways & Best Practices for Local LLMs
1. **Never Do 1-Chunk-Per-Call if Avoidable:** Grouping 100 chunks into 20 micro-batches of 5 chunks cuts latency by ~80% while still fitting comfortably inside a 2k–4k context window.
2. **Use `[SKIP]` Gating:** In a 100-chunk retrieval, chunks 30–100 often contain heavy semantic overlap with chunks 1–29. Giving the LLM an explicit `SKIP` exit prevents rambling and hallucinated filler.
3. **Alias Resolution is Crucial for Counting:** If Chunk 1 mentions *"Nijo Fish Market"* and Chunk 12 mentions *"Nijo Market"*, a naive counter will count 2. Always run a final lightweight deduplication pass over the accumulated entity list before stating the final number.