import re
import ollama
from rank_bm25 import BM25Okapi

EXPANSION_MODEL = "qwen3:1.7b"


def expand_query(query: str) -> str:
    """Uses qwen3:1.7b to generate synonyms, related technical terms, and jargon."""
    system_prompt = (
        "You are a search query expansion engine. Given a user query, output 4 to 8 "
        "relevant synonyms, related technical keywords, or alternative phrasings. "
        "Output ONLY the space-separated words on a single line. Do not explain."
    )

    response = ollama.chat(
        model=EXPANSION_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": query},
        ],
        options={
            "temperature": 0.1,
            "num_predict": 32,  # Hard ceiling to keep token generation sub-second
            "stop": ["\n", "User:", "Assistant:"],
        },
    )

    expanded_terms = response["message"]["content"].strip()
    # Clean out any non-alphanumeric noise or punctuation
    expanded_clean = re.sub(r"[^\w\s-]", " ", expanded_terms)
    return expanded_clean


def tokenize(text: str) -> list[str]:
    """Simple lowercase whitespace and word-boundary tokenizer."""
    return re.findall(r"\b\w+\b", text.lower())


class LexicalExpandedRetriever:

    def __init__(self, corpus: list[dict]):
        """Corpus format: [{'id': 0, 'text': '...'}]"""
        self.corpus = corpus
        self.tokenized_corpus = [tokenize(doc["text"]) for doc in corpus]
        self.bm25 = BM25Okapi(self.tokenized_corpus)

    def search(self, query: str, top_k: int = 15) -> list[dict]:
        # 1. Expand query with the 1.7b model
        expansion = expand_query(query)

        # 2. Combine original query (repeated for implicit weight) + expansion
        # Repeating the original query tokens ensures exact matches still rank highest
        combined_query_tokens = (
            tokenize(query) * 2 + tokenize(expansion)
        )

        # 3. Score against in-memory BM25 index
        doc_scores = self.bm25.get_scores(combined_query_tokens)

        # 4. Sort and return top candidates
        ranked_indices = sorted(
            range(len(doc_scores)), key=lambda i: doc_scores[i], reverse=True
        )[:top_k]

        return [
            {
                "id": self.corpus[idx]["id"],
                "text": self.corpus[idx]["text"],
                "bm25_score": float(doc_scores[idx]),
            }
            for idx in ranked_indices
            if doc_scores[idx] > 0.0
        ]
