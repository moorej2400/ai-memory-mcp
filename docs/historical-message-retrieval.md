# Historical message retrieval

Use message filters when you need an original conversation message, not a general summary.

```json
{
  "query": "dashboard local setup",
  "artifact_kind": "message",
  "author_name": "Morgan, Casey",
  "limit": 8
}
```

`author_name` matches the complete display name without ASCII case sensitivity.
`conversation` accepts a parent conversation artifact URI or artifact ID.
These filters apply to exact, lexical, and vector candidates.
The filters select message anchors. Context can include nearby speakers.
Recall includes `author_name` and `parent_artifact_uri` with each source result.
Use `memory_artifact_read` to verify the original text and its surrounding messages.

## Local embedding model

1. Install the optional provider in the runtime environment.

   ```sh
   python -m pip install -e '.[semantic]'
   ```

2. Download the model once, with network access.

   ```sh
   python -c 'from huggingface_hub import snapshot_download; snapshot_download("minishlab/potion-base-8M")'
   ```

3. Set these values in the private `.env` file.

   ```dotenv
   AI_MEMORY_MCP_EMBEDDING_PROVIDER=model2vec
   AI_MEMORY_MCP_EMBEDDING_MODEL=minishlab/potion-base-8M
   ```

4. Run `ai-memory-sync` to publish compatible indexes.
5. Check `memory_status` for the recorded provider and successful publication.
6. Restart the MCP client connection if its server retains the previous configuration.

Normal queries load the cached model without network access.
Queries use the provider recorded in each index until a new generation replaces it.
The `auto` setting can use hashed features when the model is unavailable.
Recall warnings identify this limited similarity mode.
A completed search does not prove that every relevant historical message was found.
