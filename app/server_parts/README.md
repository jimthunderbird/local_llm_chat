# app/server_parts

`app/chat_server.py` keeps only the process entry point (`parse_args`, server
startup) and the composed `ChatRequestHandler`. Feature logic lives here, one
module per aspect, so a change to a feature stays in one small file instead of a
40,000-line script.

The split follows the feature requirements in `prompts/*.prompt`:

| Module | Responsibility | Prompt source |
| --- | --- | --- |
| `core.py` | Shared imports, paths, command catalogue, tiny helpers | shared |
| `english_corner.py` | Vocabulary ranges and settings | `english_corner.prompt` |
| `news.py` | News article fetch, Medium feed, source queries | `news.prompt` |
| `news_research.py` | `/research_with_the_news` planning and searches | `research_with_the_news.prompt` |
| `docs.py` | MDN and Britannica search | `mdn.prompt`, `britanica.prompt` |
| `stock.py` | Quotes, ETF holdings, company profiles | `stock.prompt` |
| `robinhood.py` | Robinhood MCP client and reports | `robinhood.prompt` |
| `zillow.py` | Zillow MCP nearby listings | `zillow.prompt` |
| `podcast.py` | `/ai_news_podcast` planning | `ai_cohosted_podcast.prompt` |
| `books.py` | `/write_a_book`, PDF books, Manning ebooks, PDF graphs | `write_a_book.prompt`, `pdf_books.prompt` |
| `pdfbook.py` | PDF book trunk learning and scoped chat sessions | `pdf_books.prompt` |
| `retrieval.py` | Corpus loading, news/article learning, chunk helpers | `chat_common.prompt`, `news.prompt` |
| `tts.py` | Local text-to-speech cache and engines | `news.prompt` |
| `stackoverflow.py` | Stack Overflow search and answers | `stackoverflow.prompt` |
| `wikivoyage.py` | Wikivoyage search and full articles | `wikivoyage.prompt` |
| `map.py` | Geocoding, nearby places, restaurants, routes | `map.prompt` |
| `game.py` | Map explorer games and place modals | `game.prompt`, `games/*.prompt` |
| `changelog.py` | `.logs` summary pages | `changelog.prompt` |
| `mathtutor.py` | Math tutor uploads and practice problems | `mathtutor.prompt` |
| `command_config.py` | Command argument catalogues and documentation URLs | multiple |
| `translation.py` | Paragraph translation helpers | `translation.prompt` |
| `code.py` | Code-snippet explanation prompts | `code_snippet_block.prompt` |
| `video.py` | Video chat state, summaries, learn jobs | `youtube.prompt` |
| `image.py` | Image attachments and vision-model selection | `image.prompt` |
| `youtube.py` | Bookmarks and YouTube result helpers | `youtube.prompt` |
| `config.py` | Model names and feature limits | `main_llm_models_update.prompt` |
| `programming.py` | Coding-question and language detection | `programming*.prompt` |
| `github.py` | GitHub code-snippet fallback | `programming*.prompt` |
| `programming_docs.py` | Official Go, Rust, and PHP documentation lookups | `performance_improvement.prompt`, `programming*.prompt` |
| `moonbit.py` | MoonBit documentation index search and page extraction | `moonbit.prompt` |
| `sat1600.py` | 1600.now SAT question-bank search and indexing | `sat_1600.prompt` |
| `conversation.py` | Follow-up rewriting and conversation memory | `chat_common.prompt` |
| `answer.py` | Answer block splitting and source annotations | `chat_common.prompt` |
| `article.py` | Article loading, section routing, iterative evidence | `article_loading.prompt`, `article_based_question_and_answer.prompt` |
| `simplify.py` | Simple-English article rewrites | `article_loading.prompt` |
| `library.py` | Gutenberg, Open Library, Internet Archive | `project_gutenberg.prompt`, `openlibrary.prompt`, `internet_archive.prompt` |
| `directory_anthem.py` | Anthem provider directory | `anthem_bluecross.prompt` |
| `directory_uhc.py` | UnitedHealthcare provider directory | `united_healthcare.prompt` |
| `state.py` | Server-side `ChatState` and answer streaming | `chat_common.prompt` |
| `reddit.py` | Reddit search and thread extraction | `reddit.prompt` |
| `handler_core.py` | HTTP routes, static files, shared response helpers | shared |
| `handler_ask.py` | Question and slash-command dispatch | `chat_common.prompt` |
| `handler_directory.py` | Anthem and UnitedHealthcare requests | `anthem_bluecross.prompt`, `united_healthcare.prompt` |
| `handler_research.py` | Deep research and news research requests | `deep_research_mode.prompt`, `research_with_the_news.prompt` |
| `handler_books.py` | Book and PDF requests | `write_a_book.prompt`, `pdf_books.prompt` |
| `handler_youtube.py` | YouTube, transcript, and learn requests | `youtube.prompt` |
| `handler_commands_a.py` | News, docs, travel, map commands | `news.prompt`, `map.prompt`, `game.prompt`, `stackoverflow.prompt`, `wikivoyage.prompt` |
| `handler_commands_b.py` | Reddit, finance, library, podcast commands | `reddit.prompt`, `stock.prompt`, `robinhood.prompt`, `zillow.prompt`, `project_gutenberg.prompt`, `openlibrary.prompt`, `internet_archive.prompt`, `ai_cohosted_podcast.prompt` |
| `handler_commands_c.py` | Article, dictionary, code, video, settings commands | `article_loading.prompt`, `dictionary.prompt`, `code_snippet_block.prompt`, `programming*.prompt`, `youtube.prompt` |
| `handler_moonbit.py` | MoonBit documentation mode requests | `moonbit.prompt` |
| `handler_sat1600.py` | 1600.now SAT question-bank mode requests | `sat_1600.prompt` |

## Dependency direction

Each feature module imports `core` for shared imports and paths. Feature-to-
feature imports are explicit and placed at the bottom of the generated module so
two features can depend on each other without an import cycle. `handler_*`
modules only import the feature helpers they use and are mixed into the single
`ChatRequestHandler` in `app/chat_server.py`.

## Verification

After a change, run:

```sh
python3 -m py_compile app/chat_server.py app/server_parts/*.py
python3 app/chat_server.py --no-prewarm --port 8877
curl http://127.0.0.1:8877/api/state
```
