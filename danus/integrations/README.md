# danus/integrations — gated literature retrieval

A thin, swappable adapter over external literature search. The exported surface is
`gated_search.py`: production defaults to the legacy Danus/LeanSearch arXiv index,
while evaluation runs can select official Matlas, a dated arXiv cutoff, or no
retrieval. `matlas.py` remains the direct legacy client for compatibility tests.

```
danus/integrations/
  gated_search.py search(query, num_results=10, timeout=30) -> gated envelope
  matlas.py       direct legacy arXiv-index compatibility client
  __init__.py     re-exports search + RESULT_FIELDS
  tests/test_integrations.py
```

## Contract

- `search(...)` **never raises** — on any failure it returns the same envelope with
  `results: []` and an `error` key (empty query, `http …`, `network: …`, bad JSON, …).
- Each result contains the legacy `("title", "theorem", "arxiv_id",
  "theorem_id")` fields. Official Matlas results also carry bounded provenance.
- Sends a real `User-Agent` (Cloudflare 403s otherwise); endpoint overridable via
  `DANUS_ARXIV_INDEX_URL` or `DANUS_MATLAS_URL`.
- External bodies, result counts, and individual fields are bounded before text
  reaches a model. Evaluation configuration failures return no results.

## Exposed as

The gateway wraps it as the compatibility MCP tool
`search_arxiv_theorems(query, num_results)`. Its envelope records the actual mode,
endpoint, and result digest; all three roles receive the same gate.

## Known limitation (cross-module)

Legacy arXiv results do not include complete bibliographic metadata. Official
Matlas results do, so consumers must branch on `source_type` instead of assuming
every lead has an `arxiv_id`.

## Tests

`python -m pytest danus/integrations/` (offline; the HTTP call is mocked).
