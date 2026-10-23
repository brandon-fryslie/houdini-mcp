# Houdini MCP Server Documentation

An MCP client gets each tool's arguments as its published schema and what it
returns from its description. These documents cover what a single tool's
description does not.

| Document | Read it to learn |
|----------|------------------|
| [tool-contracts.md](tool-contracts.md) | What every tool's result shares, and the arguments callers most often get wrong |
| [remote-listener.md](remote-listener.md) | How to let an MCP server on another machine reach Houdini |
| [implementation/error-handling-patterns.md](implementation/error-handling-patterns.md) | How a tool reports a failure, for anyone writing or changing one |
| [adr/0001-language-and-process-boundaries.md](adr/0001-language-and-process-boundaries.md) | Why the server, the plugin and Houdini run where they do |
