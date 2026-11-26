"""/ai_news_podcast script planning."""

from __future__ import annotations

from .core import *

# /ai_news_podcast lists the five newest stories from the last 12 hours and
# turns their full article bodies into a two-host podcast script, read aloud
# with two distinct male voices.
AI_NEWS_PODCAST_QUERY = "when:12h news"

# latest_news_url is https://news.google.com/rss/search?q=when:12h&... ;
# Google News returns an empty feed for the bare time operator, so the command
# keeps the same 12-hour window and adds the word "news" to receive stories.
AI_NEWS_PODCAST_LIMIT = 5

AI_NEWS_PODCAST_MODEL = chat.CHAT_MODEL

AI_NEWS_PODCAST_SOURCE_CHARS = 24_000

AI_NEWS_PODCAST_NUM_PREDICT = 5_000

AI_NEWS_PODCAST_NUM_CTX = chat.ANSWER_NUM_CTX

AI_NEWS_PODCAST_PROMPT = """You are the head writer for a daily AI news podcast.

Below is the source material: up to five current news stories with their
title, source, summary, and full article paragraphs.

Write an engaging podcast episode of approximately 2000 words. It must sound
like two male co-hosts talking together: HOST leads the show and GUEST adds
analysis and context. Discuss every story in the source material, in order,
and never invent facts that are absent from the source.

Formatting rules (follow them exactly):
- Put exactly one speaker label at the start of every line: HOST: or GUEST:.
- Every spoken turn starts with one of those two labels and a space.
- Alternate naturally between HOST and GUEST; do not write stage directions,
  markdown, headings, bullet points, timestamps, or sound-effect notes.
- Do not include an episode title or a closing summary section.
- Aim for roughly 2000 words of spoken dialogue.

Source material:
{source}

Podcast script:
"""
