// Step 11 — Custom MCP server (stdio transport).
//
// A minimal Model Context Protocol server exposing ONE workflow-specific tool,
// get_sla_policy (see lib/triage/sla_policy.dart). It speaks JSON-RPC 2.0 over
// newline-delimited stdin/stdout — the stdio transport — so a host (Claude Code,
// Claude Desktop) launches it as a subprocess and no network/port/auth is
// involved.
//
// Register it with Claude Code via the project .mcp.json, then the tool is
// callable as `get_sla_policy`. Verify by hand:
//   printf '%s\n' \
//     '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
//     '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
//     '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_sla_policy","arguments":{"urgency":"urgent"}}}' \
//   | dart run bin/sla_mcp_server.dart
//
// IMPORTANT: stdout is the protocol channel — only JSON-RPC goes there. Any
// diagnostics must go to stderr, or the transport breaks.

import 'dart:convert';
import 'dart:io';

import 'package:todo_app/triage/sla_policy.dart';

const _protocolVersion = '2025-06-18';
const _serverInfo = {'name': 'support-sla', 'version': '0.1.0'};

Future<void> main() async {
  final messages =
      stdin.transform(utf8.decoder).transform(const LineSplitter());

  await for (final line in messages) {
    final trimmed = line.trim();
    if (trimmed.isEmpty) continue;

    Map<String, dynamic> msg;
    try {
      msg = jsonDecode(trimmed) as Map<String, dynamic>;
    } catch (_) {
      continue; // ignore anything that isn't a JSON-RPC message
    }

    final id = msg['id'];
    final method = msg['method'] as String?;

    // Notifications (no id) get no response — e.g. notifications/initialized.
    if (id == null) continue;

    switch (method) {
      case 'initialize':
        final clientProto =
            (msg['params'] as Map?)?['protocolVersion'] as String?;
        _reply(id, {
          // Echo the client's protocol version when given, else our default.
          'protocolVersion': clientProto ?? _protocolVersion,
          'capabilities': {'tools': <String, dynamic>{}},
          'serverInfo': _serverInfo,
        });
      case 'tools/list':
        _reply(id, {
          'tools': [slaPolicyToolSchema()],
        });
      case 'tools/call':
        _handleToolCall(id, msg['params'] as Map<String, dynamic>?);
      case 'ping':
        _reply(id, <String, dynamic>{});
      default:
        _error(id, -32601, 'Method not found: $method');
    }
  }
}

void _handleToolCall(Object id, Map<String, dynamic>? params) {
  final name = params?['name'] as String?;
  final args =
      (params?['arguments'] as Map?)?.cast<String, dynamic>() ?? const {};

  if (name != slaPolicyToolName) {
    _error(id, -32602, 'Unknown tool: $name');
    return;
  }

  try {
    final urgency = args['urgency'] as String?;
    if (urgency == null) {
      throw ArgumentError('Missing required argument: urgency');
    }
    final policy = slaPolicyFor(urgency);
    // Tool success: MCP returns the payload as content blocks.
    _reply(id, {
      'content': [
        {'type': 'text', 'text': jsonEncode(policy)},
      ],
      'isError': false,
    });
  } catch (e) {
    // Tool-level failure is a normal result with isError:true (not a
    // JSON-RPC protocol error), so the model can see and react to it.
    _reply(id, {
      'content': [
        {'type': 'text', 'text': 'Error: $e'},
      ],
      'isError': true,
    });
  }
}

void _reply(Object id, Map<String, dynamic> result) {
  stdout.writeln(jsonEncode({'jsonrpc': '2.0', 'id': id, 'result': result}));
}

void _error(Object id, int code, String message) {
  stdout.writeln(jsonEncode({
    'jsonrpc': '2.0',
    'id': id,
    'error': {'code': code, 'message': message},
  }));
}
