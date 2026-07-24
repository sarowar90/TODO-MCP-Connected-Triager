import '../../domain/entities/todo.dart';

/// Data-layer representation of [Todo] that knows how to (de)serialize to the
/// sqflite row format (a `Map<String, Object?>`).
class TodoModel extends Todo {
  const TodoModel({
    required super.id,
    required super.title,
    super.description,
    super.isCompleted,
    required super.createdAt,
  });

  /// Builds a model from a domain entity.
  factory TodoModel.fromEntity(Todo todo) {
    return TodoModel(
      id: todo.id,
      title: todo.title,
      description: todo.description,
      isCompleted: todo.isCompleted,
      createdAt: todo.createdAt,
    );
  }

  /// Builds a model from a database row.
  factory TodoModel.fromMap(Map<String, Object?> map) {
    return TodoModel(
      id: map['id'] as String,
      title: map['title'] as String,
      description: (map['description'] as String?) ?? '',
      isCompleted: (map['is_completed'] as int? ?? 0) == 1,
      createdAt: DateTime.fromMillisecondsSinceEpoch(
        map['created_at'] as int,
      ),
    );
  }

  /// Serializes to a database row.
  Map<String, Object?> toMap() {
    return {
      'id': id,
      'title': title,
      'description': description,
      'is_completed': isCompleted ? 1 : 0,
      'created_at': createdAt.millisecondsSinceEpoch,
    };
  }
}
