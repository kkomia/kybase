---
name: kylab-knowledge-base
description: Query a kylab knowledge base to answer questions from the user's own documents with traceable citations. Use this Skill whenever the user asks something that their local knowledge base might cover — product manuals, research papers, internal guidelines, subscribed feeds — or explicitly says "查知识库", "在我的文档里找", "知识库里有没有", "ask my knowledge base", "search my docs". Also use it before answering from general knowledge when the answer should be grounded in the user's own material, because kylab returns the actual source passages rather than a generated summary.
summary: 在你的知识库里检索原文段落，给出可追溯出处的回答
---

# Query a kylab knowledge base

kylab is a local, self-hosted knowledge base. Its core capability is **retrieval with
traceable sources**: `search` returns the actual passages from the user's documents
(together with document name, page, and heading path), not a generated answer. That
distinction matters — when the user needs to trust or verify an answer, the passages are
the evidence, and a summary is not.

## Standard workflow

1. Call `list_knowledge_bases` to see which libraries exist.
   **Do this first**: retrieval is scoped per library, and a library that doesn't contain
   the material will return nothing no matter how good the query is.
2. Call `search` with a natural-language query and the relevant `knowledge_base_ids`.
   Leave `knowledge_base_ids` empty only when the user genuinely wants to search
   everything.
3. Read the returned passages and answer **from them**, citing the document name
   (and page/heading when present) for each claim.
4. If a result set is empty or clearly off-topic, reformulate and search again before
   concluding the material is absent. Retrieval is keyword- and vector-sensitive:
   "眼轴监测" and "监测眼轴长度" can rank differently.

## Tools

| Tool | Use it when | Do not |
| --- | --- | --- |
| `list_knowledge_bases` | Always, at the start; and again after the user adds documents | Assume a library id — they are generated and opaque |
| `search` | The default action for answering anything | Use it as a substitute for reading the returned text; the passages are the answer's basis |
| `list_documents` | The user asks what a library contains, or you need to check whether a document finished processing | Use `search` to survey a library — it only returns passages related to a query, never the full inventory |
| `upload_document` | The user hands you a file (base64) to add | Re-upload the same content to "refresh" it — content-hash dedup returns the existing document instead |
| `add_data_source` | The user wants a feed or page kept up to date | Expect content to appear immediately — registering does **not** fetch |
| `get_document_status` | After an upload, or when search finds nothing | Assume a non-`indexed` document is searchable; it is not |
| `create_note` | You produced a conclusion, decision, or procedure worth keeping | Expect it to be searchable yet — a note is not in any library until `attach_note_to_kb` |
| `attach_note_to_kb` | The user wants a result kept in a library, or says "记到知识库" | Re-attach the same note to "refresh" it; identical content dedups |
| `list_notes` | You need to find a note again, or check what has not been filed yet | Assume a listed note is searchable — `in_knowledge_base` is the field that says so |
| `recall` | The question is about **us**: what we decided, what the user prefers, what went wrong last time | Use it as a substitute for `search` — memory has no sources to cite |
| `remember` | A durable preference, decision, or lesson came up and should survive the session | Dump a whole document into it — one reusable fact per call, and long material belongs in `create_note` |
| `delete_document` | The user explicitly asks to remove something | Treat it as reversible without saying so — the original goes to a 7-day trash, but the chunks and vectors are gone immediately, so it stops being searchable at once |
| `create_knowledge_base` | The user wants a new, separate library | Mix unrelated material into one library; separate libraries keep retrieval scoped |

## Two pools: `search` and `recall` are not interchangeable

kylab keeps **documents** and **memory** apart, and mixing them is the mistake to avoid:

| | `search` | `recall` |
| --- | --- | --- |
| Answers | "what does the material say" | "what did we say" |
| Has sources | Yes — document, page, heading; quote it | No — it came from a conversation |
| Use when | The user needs a verifiable basis | The user references prior context, or their own preferences |

Do not present recalled memory as if it were sourced evidence, and do not answer "what did we
decide" from `search` — the decision may never have been written into a document.

## Saving results back into the knowledge base

The retrieval tools answer questions; these two **keep the answer**. Use them when the
conversation produces something worth finding again later — a settled conclusion, a
decision with its reasons, a procedure that worked.

1. `create_note` with the content as Markdown (`title` optional, `tags` help later).
   **A note alone is not searchable.** Say so rather than implying it is saved.
2. `attach_note_to_kb` with the `note_id` and the target `knowledge_base_id`.
   The note becomes a Markdown document and goes through the same pipeline as an upload,
   so it is not searchable until processing finishes (`get_document_status` reports it).

Two habits worth keeping: ask before filing into a library the user did not name, and
prefer one note per distinct conclusion over one long running log — the point is that a
future search can land on it.

## Answering rules

**Ground every claim in a returned passage.** If the passages don't support an answer, say
so plainly — "the knowledge base doesn't cover this" is a useful answer. Do not fill the
gap from general knowledge without labelling it as such: the user asked their own
documents precisely because general knowledge was not the point.

**Quote or point to the source.** Give the document name; add page and heading path when
kylab returns them. A claim the user cannot trace back is not usable for their purposes.

**Stages matter.** `get_document_status` reports a pipeline stage
(`uploaded → parsing → parsed → chunked → embedding → indexed`). Only `indexed` is
searchable. When a search comes up empty, check whether the document is merely still
processing before reporting the material as missing.

## Local setup

This Skill drives the kylab MCP server, which runs beside the kylab backend.

```bash
# from the kylab repository
cd backend
python -m app.mcp_server.server --list-tools          # confirm the 11 tools are visible
python -m app.mcp_server.server --transport stdio      # what an MCP client launches
```

**Every tool call needs a credential** — there is no anonymous access. Create a key in
kylab's console (设置 → API 密钥) and give it the scope you want the agent to have:
a key bound to specific libraries can only see and write those, and a read-only key
cannot upload, file notes, or delete.

stdio has no request headers, so the key goes in the client's environment:

```json
{
  "mcpServers": {
    "kylab": {
      "command": "python",
      "args": ["-m", "app.mcp_server.server", "--transport", "stdio"],
      "cwd": "/absolute/path/to/kylab/backend",
      "env": { "KYLAB_DATA_DIR": "./data", "KYLAB_MCP_KEY": "<your API key>" }
    }
  }
}
```

For a machine on the LAN, start the HTTP transport instead and pass the key as a bearer
token on each request:

```bash
python -m app.mcp_server.server --transport http --host 0.0.0.0 --port 8765
curl -H "Authorization: Bearer <your API key>" ... 
```

Both transports accept either an API key or a **login session token**
(`kylab_st_…`). Prefer the session token when the agent should act *as the user*:
notes created through a plain API key have no account behind them, so they are filed
without an owner and will not show up in that user's own note list.

The server binds `127.0.0.1` by default on purpose. The key decides what a caller can
reach, but the tool set still includes deletion — only expose it on a network you trust.

## Proxying a plain REST client

If the agent runtime cannot speak MCP, `scripts/kylab_query.py` in this Skill calls
kylab's REST API directly and prints the passages. Same retrieval, no MCP client needed:

```bash
python scripts/kylab_query.py --base-url http://127.0.0.1:8000 --query "眼轴怎么监测"
```
