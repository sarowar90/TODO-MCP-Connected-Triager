// Step 10 — Slack MCP connector.
//
// Wiring for Slack's hosted MCP server so a triaged message can be delivered to
// the right team's channel. Like the GitHub connector (Step 9), this uses the
// Anthropic MCP connector (beta `mcp-client-2025-11-20`): the Slack connection
// is made server-side, the model calls the post tool, and the result comes back
// as `mcp_tool_result`.
//
// The *destination* is not left to the model's judgement — [slackChannelFor]
// maps the routed [Team] to a channel deterministically in code, and the router
// (see `slack_router.dart`) tells the model exactly which channel to post to.
//
// Auth: `authorizationToken` is a Slack OAuth token (e.g. a bot token,
// `xoxb-...`) sent as the Bearer credential to Slack's MCP server. Scope it to
// just `chat:write` on the workspace so the model can post but not read or
// administer.

import 'triage_taxonomy.dart';

/// Slack's hosted MCP server endpoint.
const slackMcpUrl = 'https://mcp.slack.com/mcp';

/// The `mcp_servers` entry describing Slack's server + how to auth to it.
Map<String, dynamic> slackMcpServer({
  required String authorizationToken,
  String name = 'slack',
}) =>
    {
      'type': 'url',
      'name': name,
      'url': slackMcpUrl,
      'authorization_token': authorizationToken,
    };

/// The `mcp_toolset` exposing Slack's tools, allowlisted to just posting a
/// message — the only action delivery needs. Everything else Slack's server
/// offers (reading history, admin, channel management) stays disabled.
///
/// Tool name must match what the Slack MCP server advertises; override
/// [allowedTools] if it differs.
Map<String, dynamic> slackToolset({
  String serverName = 'slack',
  List<String> allowedTools = const ['slack_post_message'],
}) =>
    {
      'type': 'mcp_toolset',
      'mcp_server_name': serverName,
      'default_config': {'enabled': false},
      'configs': [
        for (final tool in allowedTools) {'name': tool, 'enabled': true},
      ],
    };

/// Default Slack channel per owning team. Total over [Team] so routing always
/// resolves to a real destination. Override per deployment.
const Map<Team, String> defaultTeamChannels = {
  Team.billing: '#cx-billing',
  Team.engineering: '#eng-triage',
  Team.customerSuccess: '#cx-success',
  Team.trustAndSafety: '#security-escalations',
  Team.retention: '#cx-retention',
  Team.triageReview: '#triage-review',
};

/// Resolves the Slack channel for a routed [team] — deterministic, no model
/// involvement, so "the message reaches the correct destination" is a property
/// of the code, not the LLM. Falls back to the human triage channel if a custom
/// [channels] map omits the team.
String slackChannelFor(Team team, [Map<Team, String>? channels]) {
  final map = channels ?? defaultTeamChannels;
  return map[team] ?? map[Team.triageReview] ?? '#triage-review';
}
