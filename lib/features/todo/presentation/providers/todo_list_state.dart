import 'package:equatable/equatable.dart';

import '../../domain/entities/todo.dart';

enum TodoStatus { initial, loading, loaded, error }

/// Which todos to show in the list.
enum TodoFilter { all, active, completed }

/// Immutable UI state for the todo list screen.
class TodoListState extends Equatable {
  const TodoListState({
    this.status = TodoStatus.initial,
    this.todos = const [],
    this.filter = TodoFilter.all,
    this.errorMessage,
  });

  final TodoStatus status;
  final List<Todo> todos;
  final TodoFilter filter;
  final String? errorMessage;

  /// Todos after applying the active [filter].
  List<Todo> get visibleTodos {
    switch (filter) {
      case TodoFilter.active:
        return todos.where((t) => !t.isCompleted).toList();
      case TodoFilter.completed:
        return todos.where((t) => t.isCompleted).toList();
      case TodoFilter.all:
        return todos;
    }
  }

  int get activeCount => todos.where((t) => !t.isCompleted).length;

  TodoListState copyWith({
    TodoStatus? status,
    List<Todo>? todos,
    TodoFilter? filter,
    String? errorMessage,
  }) {
    return TodoListState(
      status: status ?? this.status,
      todos: todos ?? this.todos,
      filter: filter ?? this.filter,
      errorMessage: errorMessage,
    );
  }

  @override
  List<Object?> get props => [status, todos, filter, errorMessage];
}
