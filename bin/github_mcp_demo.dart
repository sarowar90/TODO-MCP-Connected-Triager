// Step 9 — GitHub MCP end-to-end demo.
//
// Triages a technical bug report with GitHub's MCP server wired in, so Claude
// can search existing issues and file a new one straight from the message. The
// MCP connection is made server-side by Anthropic (beta `mcp-client-2025-11-20`);
// we just supply the server + a GitHub token and print what the model did.
//
// Needs an Anthropic key AND a GitHub token (a fine-grained PAT scoped to
// Issues: read/write on the target repo). Optionally set GITHUB_REPO=owner/name.
//   PowerShell:  $env:ANTHROPIC_API_KEY="sk-ant-..."; $env:GITHUB_TOKEN="github_pat_..."; $env:GITHUB_REPO="owner/repo"; dart run bin/github_mcp_demo.dart
//   Bash:        ANTHROPIC_API_KEY=sk-ant-... GITHUB_TOKEN=github_pat_... GITHUB_REPO=owner/repo dart run bin/github_mcp_demo.dart

import 'dart:io';

import 'package:todo_app/triage/anthropic_client.dart';
import 'package:todo_app/triage/github_mcp.dart';
import 'package:todo_app/triage/tools.dart';
import 'package:todo_app/triage/triage_service.dart';

const _defaultRepo = 'sarowar90/TODO-MCP-Connected-Triager';

const _sampleMessage =
    'Bug report from jane@example.com: the CSV export button on the reports '
    'page does nothing — no download, no error. Console shows a 500 from '
    '/api/reports/export. This blocks our weekly reporting. Please fix.';

Future<void> main() async {
  final apiKey = Platform.environment['ANTHROPIC_API_KEY'];
  final githubToken = Platform.environment['GITHUB_TOKEN'];
  final repo = Platform.environment['GITHUB_REPO'] ?? _defaultRepo;

  if (apiKey == null || apiKey.isEmpty) {
    stderr.writeln('ERROR: ANTHROPIC_API_KEY is not set.');
    exitCode = 1;
    return;
  }
  if (githubToken == null || githubToken.isEmpty) {
    stderr.writeln(
      'ERROR: GITHUB_TOKEN is not set. Provide a GitHub PAT scoped to '
      "Issues: read/write on $repo (the model can't exceed that scope).",
    );
    exitCode = 1;
    return;
  }

  final client = AnthropicClient(apiKey: apiKey);
  final executor = MockToolExecutor();
  // Wire the GitHub MCP connector into the triager: the server + auth, the
  // allowlisted issue toolset, and the guidance telling triage when to use it.
  final service = TriageService(
    api: client,
    executor: executor,
    mcpServers: [githubMcpServer(authorizationToken: githubToken)],
    mcpToolsets: [githubIssueToolset()],
    mcpGuidance: githubIssueGuidance(repo),
  );

  stdout.writeln('Repo:    $repo');
  stdout.writeln('Message:\n  $_sampleMessage\n');
  stdout.writeln('Triaging (GitHub MCP connected)...\n');

  try {
    final outcome = await service.triage(_sampleMessage);

    stdout.writeln('Tool calls Claude made (client + GitHub MCP):');
    for (final call in outcome.toolCalls) {
      stdout.writeln('  • $call');
    }

    stdout.writeln('\nFinal triage: ${outcome.result}');
    stdout.writeln('  summary:   ${outcome.result.summary}');
    stdout.writeln('  rationale: ${outcome.result.rationale}');
    stdout.writeln('  ticket:    ${outcome.ticketId}');
    stdout.writeln('  tier:      ${outcome.tierUsed.label}'
        '${outcome.escalated ? ' (escalated)' : ''}');

    // Surface the GitHub issue action specifically — the end-to-end result.
    final githubCalls =
        outcome.toolCalls.where((c) => c.name.startsWith('github/')).toList();
    if (githubCalls.isEmpty) {
      stdout.writeln('\n(No GitHub issue action — not classified as a bug.)');
    } else {
      stdout.writeln('\nGitHub issue actions:');
      for (final c in githubCalls) {
        stdout.writeln('  • ${c.name}(${c.input}) -> ${c.resultContent}');
      }
    }
  } on AnthropicException catch (e) {
    stderr.writeln('API error (HTTP ${e.statusCode}): ${e.message}');
    exitCode = 1;
  } finally {
    client.close();
  }
}
