// Step 9 — GitHub MCP connector.
//
// Wiring for GitHub's hosted MCP server so the triager can look up and file
// issues straight from a support message. This uses the Anthropic **MCP
// connector** (beta `mcp-client-2025-11-20`): the connection to GitHub is made
// server-side by Anthropic — the model emits `mcp_tool_use` blocks and the
// results come back as `mcp_tool_result` in the same response, so there is
// nothing for the local [ToolExecutor] to run.
//
// Two pieces are required together on a request (see `anthropic_client.dart`):
//   • an entry in `mcp_servers` — where to connect and how to authenticate;
//   • an `mcp_toolset` in `tools` — which server's tools the model may call.
//
// Auth: `authorizationToken` is a GitHub token sent as the Bearer credential to
// the remote server — NOT the Anthropic key. Provision a fine-grained PAT scoped
// to **Issues: read and write** on the target repo only; that token scope, not
// the toolset list, is the real blast-radius limit if the model misbehaves.

/// GitHub's hosted MCP server endpoint.
const githubMcpUrl = 'https://api.githubcopilot.com/mcp/';

/// The `mcp_servers` entry describing GitHub's server + how to auth to it.
///
/// [authorizationToken] is a GitHub PAT (Issues: read/write). [name] is the
/// handle the [githubIssueToolset] references — keep them in sync.
Map<String, dynamic> githubMcpServer({
  required String authorizationToken,
  String name = 'github',
}) =>
    {
      'type': 'url',
      'name': name,
      'url': githubMcpUrl,
      'authorization_token': authorizationToken,
    };

/// The `mcp_toolset` entry that exposes GitHub's tools to the model, allowlisted
/// to the issue tools triage actually needs: search existing issues (dedup),
/// read one, and create a new one. Everything else on the GitHub server stays
/// disabled, so the model can't wander into PRs, workflows, or repo settings —
/// and it keeps the tool schemas out of the context window.
///
/// Tool names must match what the GitHub MCP server advertises; adjust
/// [allowedTools] if the server renames them.
Map<String, dynamic> githubIssueToolset({
  String serverName = 'github',
  List<String> allowedTools = const [
    'search_issues',
    'get_issue',
    'create_issue',
  ],
}) =>
    {
      'type': 'mcp_toolset',
      'mcp_server_name': serverName,
      'default_config': {'enabled': false},
      'configs': [
        for (final tool in allowedTools) {'name': tool, 'enabled': true},
      ],
    };

/// System-prompt guidance describing how the triager should use the GitHub
/// issue tools. Passed to [TriageService.mcpGuidance] so it's appended only when
/// the connector is actually configured. [repo] is `owner/name`.
String githubIssueGuidance(String repo) => '''

You can look up and file GitHub issues in $repo via the github tools. For a
message you classify as a genuine technical bug: first search existing issues to
avoid duplicates; if a matching open issue exists, reference its number in your
rationale instead of filing a new one; if none matches, create one with a clear
title and a body summarizing the report, then reference the new issue's number.
Do not open issues for how_to questions, billing, or feature requests.''';
