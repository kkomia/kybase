---
name: kylab-knowledge-base
description: Query a kybase knowledge base to answer questions from the user's own documents with traceable citations. Use this Skill whenever the user asks something that their local knowledge base might cover — product manuals, research papers, internal guidelines, subscribed feeds — or explicitly says "查知识库", "在我的文档里找", "知识库里有没有", "ask my knowledge base", "search my docs". Also use it before answering from general knowledge when the answer should be grounded in the user's own material, because kybase returns the actual source passages rather than a generated summary.
summary: 在你的知识库里检索原文段落，给出可追溯出处的回答
---

# Query a kybase knowledge base

kybase is a self-hosted knowledge base service. Its core capability is **retrieval with
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
| `get_document_status` | After an upload, or when search finds nothing | Assume a non-`indexed` document is searchable; it is not |
| `get_document_content` | You need the document itself rather than the passages one query matched — `search` answers "what does the base say about X", this answers "what does this document say". `format="markdown"` (default) returns the parsed body text; `format="original"` returns the original file as base64 with its `filename` / `media_type` / `size_bytes` | Use it as a substitute for `search` — a whole document in the context is rarely necessary and long ones are not readable. Markdown over 200 000 characters comes back cut off with `truncated=true`: **that is not the whole document**, so never conclude something is absent from what the cut copy omits. An original file over 32 MB is refused outright (the error points at the REST download endpoint) instead of truncated |
| `upload_document` | The user hands you a file (base64) to add — or a conclusion, decision, or procedure is worth keeping and the user wants it searchable | Re-upload the same content to "refresh" it (content-hash dedup returns the existing document instead), and do not expect it to be searchable at once — it goes through the same pipeline as any upload, so check `get_document_status` |
| `add_data_source` | The user wants a feed or page kept up to date | Expect content to appear immediately — registering does **not** fetch |
| `create_knowledge_base` | The user wants a new, separate library | Mix unrelated material into one library; separate libraries keep retrieval scoped |
| `delete_document` | The user explicitly asks to remove something | Treat it as reversible without saying so — the original goes to a 7-day trash, but the chunks and vectors are gone immediately, so it stops being searchable at once |

**This table is the whole surface.** Read the live list with
`python -m app.mcp_server.server --list-tools` rather than trusting any count written down —
the set grows, and a remembered number is how you end up believing a tool is missing.

## Retrieval only: no memory, no notes

This service serves **documents**, and the table above is its entire surface. There is no
memory, no notes, and no conversation log here — `recall` / `remember` / `create_note` /
`attach_note_to_kb` belong to the Agent side, and their implementations are not part of this
codebase at all (they were removed when the service was slimmed down to the knowledge base
itself, 2026-10-09).

The practical consequence: "what did we decide?" has exactly one honest answer here — if the
decision was written into a document, `search` finds it and you can cite it; if it was not,
this service cannot answer it, and you should say so rather than presenting a guess as
sourced evidence.

## Saving results back into the knowledge base

The retrieval tools answer questions; `upload_document` **keeps the answer**. Use it when the
conversation produces something worth finding again later — a settled conclusion, a
decision with its reasons, a procedure that worked.

Write it as Markdown and send the content as base64 with a `filename` that ends in `.md`
(the pipeline dispatches on the extension). It goes through the same pipeline as any
other upload, so it is **not searchable until processing finishes** — say that plainly, and
use `get_document_status` to report progress.

Two habits worth keeping: ask before filing into a library the user did not name, and prefer
one document per distinct conclusion over one long running log — the point is that a future
search can land on it. (`create_note` / `attach_note_to_kb` are **not** on this service —
they belong to the Agent side; `upload_document` is how content gets in here.)

## Answering rules

**Ground every claim in a returned passage.** If the passages don't support an answer, say
so plainly — "the knowledge base doesn't cover this" is a useful answer. Do not fill the
gap from general knowledge without labelling it as such: the user asked their own
documents precisely because general knowledge was not the point.

**Quote or point to the source.** Give the document name; add page and heading path when
the service returns them. A claim the user cannot trace back is not usable for their purposes.

**Stages matter.** `get_document_status` reports a pipeline stage
(`uploaded → parsing → parsed → chunked → embedding → indexed`). Only `indexed` is
searchable. When a search comes up empty, check whether the document is merely still
processing before reporting the material as missing.

## Local setup

This Skill drives the kybase MCP server, which runs beside the kybase backend. The Skill
ships inside the kybase repository (`skills/kylab-knowledge-base/`), so that backend is the
`backend/` directory next to it.

```bash
# in the kybase repository's backend directory
cd backend
python -m app.mcp_server.server --list-tools          # the tool names it exposes — read the list, don't match a remembered count (the set grows)
python -m app.mcp_server.server --transport stdio      # what an MCP client launches
```

**Every tool call needs a credential** — there is no anonymous access. Create a key on
kybase's console, on the sidebar's **API 密钥** page (it takes an admin session), and give
it the scope you want the agent to have: a key bound to specific libraries can only see and
write those, a read-only key cannot upload, create libraries, or delete, and a key can be
**limited to a subset of the tools** — scope it to `search` and it can query, and nothing
else. Leaving the library scope or the tool scope empty means "unrestricted" on that axis;
`permission` is the one axis that is always read-only or read/write.

stdio has no request headers, so the key goes in the client's environment:

```json
{
  "mcpServers": {
    "kybase": {
      "command": "python",
      "args": ["-m", "app.mcp_server.server", "--transport", "stdio"],
      "cwd": "/absolute/path/to/kybase/backend",
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

The instance deployed on the LAN answers at `http://192.168.31.18:8765/mcp`; **another
deployment gives you its own address, so take that from whoever deployed it.**

Both transports accept either an API key or a **login session token**
(`kylab_st_…`). Prefer the session token when the agent should act *as the user*: a library
created through a plain API key has no owner behind it (`owner_id` is `NULL`), so a plain
member cannot manage it afterwards — sharing included.

The server binds `127.0.0.1` by default on purpose. The key decides what a caller can
reach, but the tool set still includes deletion — only expose it on a network you trust.

## Proxying a plain REST client

If the agent runtime cannot speak MCP, `scripts/kylab_query.py` in this Skill calls the
REST API directly and prints the passages. Same retrieval, no MCP client needed:

```bash
python scripts/kylab_query.py --base-url http://127.0.0.1:8000 --query "眼轴怎么监测"
# the LAN instance answers at http://192.168.31.18:8081/api/v1 (the deployer's value wins)
```
