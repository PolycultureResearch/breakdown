// Bridge Claude Desktop (stdio) to a breakdown server's /mcp endpoint
// (streamable HTTP), via the bundled mcp-remote proxy.
//
// This file exists instead of pointing mcp_config straight at mcp-remote for
// one reason: the API token is optional, and an empty token must mean "send
// no Authorization header at all", not "send `Bearer ` with nothing after
// it". The manifest can only substitute user_config values into fixed
// argument lists, so the conditional lives here.
//
// Claude Desktop runs this with its own bundled Node; nothing needs to be
// installed on the user's machine.

const { spawn } = require("child_process");
const path = require("path");

const url = (process.env.BREAKDOWN_MCP_URL || "http://127.0.0.1:9090/mcp").trim();
const token = (process.env.BREAKDOWN_MCP_TOKEN || "").trim();

const proxy = path.join(__dirname, "..", "node_modules", "mcp-remote", "dist", "proxy.js");
const args = [proxy, url];
if (token) {
  args.push("--header", `Authorization: Bearer ${token}`);
}
// A local plain-HTTP server is the default setup; mcp-remote refuses
// non-localhost http on its own, so this stays safe for remote URLs.
if (url.startsWith("http://")) {
  args.push("--allow-http");
}

const child = spawn(process.execPath, args, { stdio: "inherit" });
child.on("exit", (code, signal) => {
  process.exit(signal ? 1 : (code ?? 1));
});
child.on("error", (err) => {
  console.error(`breakdown connector: failed to start mcp-remote: ${err.message}`);
  process.exit(1);
});
