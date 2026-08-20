# The Claude Desktop extension (.mcpb)

A one-click connector for Claude Desktop: download `breakdown.mcpb` from a
GitHub release, double-click it, fill in two fields (server URL, optional API
token), restart Claude. No Node install, no JSON editing. Claude Desktop runs
the bundle with its own built-in Node runtime.

What's inside: `server/index.js` bridges Claude's stdio transport to a
breakdown server's `/mcp` endpoint through a vendored
[`mcp-remote`](https://www.npmjs.com/package/mcp-remote). The entry script
exists so an empty token means "send no Authorization header", which a fixed
argument list in the manifest cannot express. `manifest.json` declares the two
`user_config` fields Claude Desktop renders as a form.

## Build

```bash
cd packaging/mcpb
npm ci --omit=dev
npx -y @anthropic-ai/mcpb validate manifest.json
npx -y @anthropic-ai/mcpb pack . ../../dist/breakdown.mcpb
```

CI does this on every GitHub release and attaches the bundle as a release
asset, stamping `manifest.json`'s `version` from the release tag
(see `.github/workflows/publish.yml`). The version committed here is only a
fallback for local builds.

When to reach for this instead of a [custom
connector](../../docs/mcp.md#connecting): the server is on localhost (a
connector runs from Anthropic's cloud and cannot reach it), or it is gated by
`BREAKDOWN_API_TOKEN` and you are not on a Team/Enterprise plan where an admin
can enter the header.

---

*This document is written and maintained by an AI agent (Claude), with human oversight.*
