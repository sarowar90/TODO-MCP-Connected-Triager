// Step 10 — Slack MCP routing demo.
//
// Triages a support message, then delivers the result to the routed team's
// Slack channel via the Slack MCP connector — and prints where it landed. The
// channel is chosen deterministically from the team (slackChannelFor), so the
// destination is guaranteed correct regardless of the model.
//
// Needs an Anthropic key AND a Slack token (a bot token with chat:write).
//   PowerShell:  $env:ANTHROPIC_API_KEY="sk-ant-..."; $env:SLACK_TOKEN="xoxb-..."; dart run bin/slack_mcp_demo.dart
//   Bash:        ANTHROPIC_API_KEY=sk-ant-... SLACK_TOKEN=xoxb-... dart run bin/slack_mcp_demo.dart

import 'dart:io';

import 'package:todo_app/triage/anthropic_client.dart';
import 'package:todo_app/triage/slack_mcp.dart';
import 'package:todo_app/triage/slack_router.dart';
import 'package:todo_app/triage/tools.dart';
import 'package:todo_app/triage/triage_service.dart';

// A cancellation threat — should classify as churn -> Retention -> #cx-retention.
const _sampleMessage =
    "This is the third billing error in two months and I'm done. Cancel my "
    'subscription and downgrade us — we are moving to a competitor unless '
    'someone fixes this today. — jane@example.com';

Future<void> main() async {
  final apiKey = Platform.environment['ANTHROPIC_API_KEY'];
  final slackToken = Platform.environment['SLACK_TOKEN'];

  if (apiKey == null || apiKey.isEmpty) {
    stderr.writeln('ERROR: ANTHROPIC_API_KEY is not set.');
    exitCode = 1;
    return;
  }
  if (slackToken == null || slackToken.isEmpty) {
    stderr.writeln('ERROR: SLACK_TOKEN is not set. Provide a Slack bot token '
        'with chat:write.');
    exitCode = 1;
    return;
  }

  final client = AnthropicClient(apiKey: apiKey);
  final service = TriageService(api: client, executor: MockToolExecutor());
  final router = SlackRouter(api: client, authorizationToken: slackToken);

  stdout.writeln('Message:\n  $_sampleMessage\n');

  try {
    // 1. Classify.
    final outcome = await service.triage(_sampleMessage);
    stdout.writeln('Triage: ${outcome.result}');
    stdout.writeln('  team:   ${outcome.result.team.label}');
    stdout.writeln('  ticket: ${outcome.ticketId}\n');

    // 2. Deliver to the correct Slack channel (deterministic from the team).
    final expected = slackChannelFor(outcome.result.team);
    stdout.writeln('Routing to Slack channel: $expected ...');
    final delivery = await router.route(outcome);

    stdout.writeln('  posted:            ${delivery.posted}');
    stdout.writeln('  channel:           ${delivery.channel}');
    stdout.writeln('  slack tool input:  ${delivery.toolInput}');
    stdout.writeln('  slack result:      ${delivery.resultContent}');
    stdout.writeln('  correct destination? '
        '${delivery.reachedCorrectDestination}');
  } on AnthropicException catch (e) {
    stderr.writeln('API error (HTTP ${e.statusCode}): ${e.message}');
    exitCode = 1;
  } finally {
    client.close();
  }
}
