# Acknowledgements

## Research
The memory design stands on published work: Generative Agents, MemoryBank, FadeMem,
MemGPT, Zep/Graphiti, A-MEM. Full citations, including which claims our verification
refuted: [docs/REFERENCES.md](docs/REFERENCES.md).

## Open source
This project is MIT-licensed and built on permissively-licensed open source
(installed via pip, not vendored; licences checked for compatibility: MIT, BSD,
Apache-2.0, PSF, MPL-2.0):

- [FastAPI](https://fastapi.tiangolo.com/) (MIT): HTTP API
- [Uvicorn](https://www.uvicorn.org/) (BSD-3-Clause): ASGI server
- [Pydantic](https://docs.pydantic.dev/) (MIT): config & request validation
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (MIT): the MCP adapter
- [httpx](https://www.python-httpx.org/) (BSD-3-Clause): HTTP client
- [Anthropic](https://github.com/anthropics/anthropic-sdk-python) / [OpenAI](https://github.com/openai/openai-python) Python SDKs (MIT / Apache-2.0)
- [pypdf](https://github.com/py-pdf/pypdf) (BSD-3-Clause): text extraction from PDF attachments, so they're searchable
- [SQLite](https://sqlite.org/) (public domain): every byte of state
- [pytest](https://pytest.org/) (MIT): the test suite

Model providers (paid APIs, bring your own keys): Anthropic, OpenAI.
