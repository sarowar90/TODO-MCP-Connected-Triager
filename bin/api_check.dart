// Step 1 — Anthropic API connectivity check.
//
// Confirms your API key works by making one simple Messages API call.
// Anthropic has no official Dart SDK, so we call the REST API directly.
//
// Run it with your key in the environment:
//   Windows PowerShell:  $env:ANTHROPIC_API_KEY="sk-ant-..."; dart run bin/api_check.dart
//   macOS/Linux/Git Bash: ANTHROPIC_API_KEY="sk-ant-..." dart run bin/api_check.dart

import 'dart:convert';
import 'dart:io';

import 'package:http/http.dart' as http;

/// The model the triager will be built on. Opus 4.8 is Anthropic's most
/// capable model — a good default for classification/triage quality.
const _model = 'claude-opus-4-8';

const _apiUrl = 'https://api.anthropic.com/v1/messages';
const _apiVersion = '2023-06-01';

Future<void> main() async {
  final apiKey = Platform.environment['ANTHROPIC_API_KEY'];
  if (apiKey == null || apiKey.isEmpty) {
    stderr.writeln(
      'ERROR: ANTHROPIC_API_KEY is not set.\n'
      'Get a key at https://console.anthropic.com (Settings → API Keys),\n'
      'then set it in your environment before running this script.',
    );
    exitCode = 1;
    return;
  }

  stdout.writeln('Calling the Anthropic Messages API ($_model)...\n');

  try {
    final reply = await sendMessage(
      apiKey: apiKey,
      prompt: 'Reply with exactly: "API connection successful."',
    );
    stdout.writeln('Claude replied: $reply');
    stdout.writeln('\n✅ Your API access is working. Foundation is ready.');
  } on ApiException catch (e) {
    stderr.writeln('❌ API call failed (HTTP ${e.statusCode}): ${e.message}');
    exitCode = 1;
  } catch (e) {
    stderr.writeln('❌ Unexpected error: $e');
    exitCode = 1;
  }
}

/// Sends a single-turn message to Claude and returns the concatenated text
/// from the response. This is the primitive the customer-support triager will
/// build on in later steps.
Future<String> sendMessage({
  required String apiKey,
  required String prompt,
  int maxTokens = 1024,
}) async {
  final response = await http.post(
    Uri.parse(_apiUrl),
    headers: {
      'content-type': 'application/json',
      'x-api-key': apiKey,
      'anthropic-version': _apiVersion,
    },
    body: jsonEncode({
      'model': _model,
      'max_tokens': maxTokens,
      'messages': [
        {'role': 'user', 'content': prompt},
      ],
    }),
  );

  if (response.statusCode != 200) {
    // The API returns a JSON error body: {"error": {"type": ..., "message": ...}}
    String message = response.body;
    try {
      final decoded = jsonDecode(response.body) as Map<String, dynamic>;
      message = (decoded['error']?['message'] as String?) ?? response.body;
    } catch (_) {
      // Non-JSON body — fall back to the raw text.
    }
    throw ApiException(response.statusCode, message);
  }

  final decoded = jsonDecode(response.body) as Map<String, dynamic>;

  // `content` is a list of blocks; collect the text from every text block.
  final content = decoded['content'] as List<dynamic>;
  final text = content
      .whereType<Map<String, dynamic>>()
      .where((block) => block['type'] == 'text')
      .map((block) => block['text'] as String)
      .join();

  return text.trim();
}

/// Thrown when the API responds with a non-200 status.
class ApiException implements Exception {
  ApiException(this.statusCode, this.message);

  final int statusCode;
  final String message;

  @override
  String toString() => 'ApiException($statusCode): $message';
}
