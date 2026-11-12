// Generic stdio shim: node mcp_relay.js -- <command> <args...>
// Forwards raw bytes; injects an initialize handshake when the client skips it.
//
// Why: some MCP clients (e.g. the dsh `mcp-registry` bridge) send their first
// tools/call without an `initialize` handshake, which strict servers reject
// with -32602. This shim performs the handshake itself, then replays any
// queued client messages. Works with any stdio MCP server, not just this one.
const fs = require('fs');
const { spawn } = require('child_process');

const dash = process.argv.indexOf('--');
if (dash < 0 || !process.argv[dash + 1]) {
  console.error('usage: node mcp_relay.js -- <command> [args...]');
  process.exit(2);
}
const cmd = process.argv[dash + 1];
const args = process.argv.slice(dash + 2);

const LOG = process.env.RELAY_LOG || ''; // optional debug log
function log(tag, s) {
  if (!LOG) return;
  try { fs.appendFileSync(LOG, tag + ' ' + String(s).replace(/\n/g, ' ').slice(0, 300) + '\n'); } catch {}
}

const child = spawn(cmd, args, { env: process.env });

let decided = false; // saw the client's first frame?
let ready = false;   // server answered our injected initialize?
const queue = [];
const shimId = 'init-' + process.pid;

child.stdout.on('data', (chunk) => {
  log('S->C', chunk);
  if (!ready && chunk.toString('utf8').includes(shimId)) {
    ready = true;
    child.stdin.write(JSON.stringify({ jsonrpc: '2.0', method: 'notifications/initialized' }) + '\n');
    for (const q of queue.splice(0)) child.stdin.write(q);
  }
  process.stdout.write(chunk);
});
child.stderr.on('data', (chunk) => log('S-ERR', chunk));

process.stdin.on('data', (chunk) => {
  const text = chunk.toString('utf8');
  log('C->S', text);
  if (!decided) {
    decided = true;
    if (text.includes('"method":"initialize"')) {
      ready = true; // client behaves; pure passthrough
      child.stdin.write(chunk);
      return;
    }
    // client skipped the handshake -> do it ourselves, queue the frame
    child.stdin.write(JSON.stringify({
      jsonrpc: '2.0', id: shimId, method: 'initialize',
      params: { protocolVersion: '2024-11-05', capabilities: {}, clientInfo: { name: 'mcp-relay-shim', version: '0.2.0' } },
    }) + '\n');
    queue.push(chunk);
    return;
  }
  if (ready) child.stdin.write(chunk);
  else queue.push(chunk);
});

child.on('exit', () => process.exit(0));
process.on('exit', () => { try { child.kill(); } catch {} });
