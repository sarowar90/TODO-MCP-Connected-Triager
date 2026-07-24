/// Thrown by the data sources when a local database operation fails.
class DatabaseException implements Exception {
  DatabaseException([this.message = 'A database error occurred.']);

  final String message;

  @override
  String toString() => 'DatabaseException: $message';
}
