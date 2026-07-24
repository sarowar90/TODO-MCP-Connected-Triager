// Step 4 — Live triage demo.
//
// Runs the full triager against the real API on a sample message, printing the
// tool-call trace and the final classification. Needs a key:
//   Windows PowerShell:   $env:ANTHROPIC_API_KEY="sk-ant-..."; dart run bin/triage_demo.dart
//   macOS/Linux/Git Bash: ANTHROPIC_API_KEY="sk-ant-..." dart run bin/triage_demo.dart

import 'dart:io';

import 'package:todo_app/triage/anthropic_client.dart';
import 'package:todo_app/triage/tools.dart';
import 'package:todo_app/triage/triage_service.dart';

const _sampleMessage =
    "Hi, this is jane@example.com. I was charged twice for order ORD-1002 this "
    'month — about \$240 total instead of \$120. Can you refund the duplicate '
    'charge?';

Future<void> main() async {
  final apiKey = Platform.environment['ANTHROPIC_API_KEY'];
  if (apiKey == null || apiKey.isEmpty) {
    stderr.writeln('ERROR: ANTHROPIC_API_KEY is not set.');
    exitCode = 1;
    return;
  }

  final client = AnthropicClient(apiKey: apiKey);
  final executor = MockToolExecutor();
  final service = TriageService(api: client, executor: executor);

  stdout.writeln('Message:\n  $_sampleMessage\n');
  stdout.writeln('Triaging (streaming live)...\n');

  try {
    // Stream the model's narration and tool activity to the screen as it happens.
    final outcome = await service.triage(
      _sampleMessage,
      onTextDelta: (text) => stdout.write(text),
      onToolUseStart: (name) => stdout.write('\n  [calling $name...]\n'),
    );

    stdout.writeln('\n\nTool calls Claude made:');
    for (final call in outcome.toolCalls) {
      stdout.writeln('  • $call');
    }

    stdout.writeln('\nFinal triage: ${outcome.result}');
    stdout.writeln('  summary:   ${outcome.result.summary}');
    stdout.writeln('  rationale: ${outcome.result.rationale}');
    stdout.writeln('  ticket:    ${outcome.ticketId}');
    stdout.writeln('  tier:      ${outcome.tierUsed.label}'
        '${outcome.escalated ? ' (escalated)' : ''}');
  } on AnthropicException catch (e) {
    stderr.writeln('API error (HTTP ${e.statusCode}): ${e.message}');
    exitCode = 1;
  } finally {
    client.close();
  }
}
