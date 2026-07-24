import 'package:equatable/equatable.dart';

/// Base class for all failures in the app.
///
/// Failures are returned from the domain/data layers (via `Either`) instead of
/// throwing exceptions, so the presentation layer can handle them gracefully.
abstract class Failure extends Equatable {
  const Failure(this.message);

  final String message;

  @override
  List<Object?> get props => [message];
}

/// A failure originating from the local database (sqflite).
class DatabaseFailure extends Failure {
  const DatabaseFailure([super.message = 'A database error occurred.']);
}

/// A failure for anything unexpected.
class UnexpectedFailure extends Failure {
  const UnexpectedFailure([super.message = 'An unexpected error occurred.']);
}
