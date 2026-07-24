// Step 11 — Custom MCP tool logic: SLA / escalation policy lookup.
//
// The workflow-specific knowledge exposed by our custom MCP server
// (`bin/sla_mcp_server.dart`): given a triage urgency level, what is the
// first-response SLA, does it page on-call, and what's the escalation step?
// Kept here (not in the server) so it's pure, importable, and unit-tested;
// the server is just a stdio transport shell over `slaPolicyFor`.

import 'triage_taxonomy.dart';

/// The single tool our custom MCP server exposes.
const slaPolicyToolName = 'get_sla_policy';

/// The tool definition in MCP's shape (note `inputSchema`, not the Messages
/// API's `input_schema`). The `urgency` enum is pulled from [Urgency] so it
/// can't drift from the taxonomy.
Map<String, dynamic> slaPolicyToolSchema() => {
      'name': slaPolicyToolName,
      'description':
          'Look up the support SLA and escalation policy for a triage urgency '
              'level. Returns the first-response target, whether it pages '
              'on-call, and the escalation step — use it after classifying a '
              'ticket to attach the right SLA and paging decision.',
      'inputSchema': {
        'type': 'object',
        'properties': {
          'urgency': {
            'type': 'string',
            'enum': [for (final u in Urgency.values) u.wireName],
            'description': 'The triage urgency level.',
          },
        },
        'required': ['urgency'],
        'additionalProperties': false,
      },
    };

/// Resolves the SLA + escalation policy for an [Urgency] wire name. Throws
/// [ArgumentError] on anything off-taxonomy, so the server surfaces a tool error
/// rather than inventing a policy.
Map<String, dynamic> slaPolicyFor(String urgencyWire) {
  final urgency = Urgency.values.firstWhere(
    (u) => u.wireName == urgencyWire,
    orElse: () => throw ArgumentError('Unknown urgency: $urgencyWire'),
  );

  // Paging + escalation are our operational policy (the SLA target itself lives
  // on the taxonomy). This is the knowledge the tool adds on top of triage.
  final (pages, escalation) = switch (urgency) {
    Urgency.urgent => (
        true,
        'Page on-call immediately; open a Sev-1 incident if there is an active '
            'outage or security exposure.',
      ),
    Urgency.high => (
        false,
        'Assign within the hour; notify the team lead if still unowned after 2 '
            'hours.',
      ),
    Urgency.normal => (false, 'Standard queue; no paging.'),
    Urgency.low => (false, 'Backlog; handle on a best-effort basis.'),
  };

  return {
    'urgency': urgency.wireName,
    'label': urgency.label,
    'first_response_target': urgency.slaTarget,
    'pages_on_call': pages,
    'escalation': escalation,
  };
}
