- Verbatim transcript search now needs the owner token on every path
  (#81). The MCP server's search_history refuses when the server was
  registered without MEMORY_AUTH_TOKEN, matching the HTTP gate; recall,
  summary and save are unchanged. Re-register the server with the token
  to keep search available to your own tools.
