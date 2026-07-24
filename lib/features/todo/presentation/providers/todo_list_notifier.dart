// Named constructor params can't be private, so they're mapped to private
// fields in the initializer list — which trips this lint spuriously.
// ignore_for_file: prefer_initializing_formals

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:uuid/uuid.dart';

import '../../domain/entities/todo.dart';
import '../../domain/usecases/add_todo.dart';
import '../../domain/usecases/delete_todo.dart';
import '../../domain/usecases/get_todos.dart';
import '../../domain/usecases/update_todo.dart';
import 'todo_list_state.dart';

/// Orchestrates todo use cases and exposes an immutable [TodoListState] to the
/// UI. All mutations re-fold the `Either` result into either an error message
/// or an updated list.
class TodoListNotifier extends StateNotifier<TodoListState> {
  TodoListNotifier({
    required GetTodos getTodos,
    required AddTodo addTodo,
    required UpdateTodo updateTodo,
    required DeleteTodo deleteTodo,
  })  : _getTodos = getTodos,
        _addTodo = addTodo,
        _updateTodo = updateTodo,
        _deleteTodo = deleteTodo,
        super(const TodoListState());

  final GetTodos _getTodos;
  final AddTodo _addTodo;
  final UpdateTodo _updateTodo;
  final DeleteTodo _deleteTodo;

  static const _uuid = Uuid();

  Future<void> loadTodos() async {
    state = state.copyWith(status: TodoStatus.loading);
    final result = await _getTodos();
    result.fold(
      (failure) => state = state.copyWith(
        status: TodoStatus.error,
        errorMessage: failure.message,
      ),
      (todos) => state = state.copyWith(
        status: TodoStatus.loaded,
        todos: todos,
      ),
    );
  }

  Future<void> addTodo({
    required String title,
    String description = '',
  }) async {
    final todo = Todo(
      id: _uuid.v4(),
      title: title.trim(),
      description: description.trim(),
      createdAt: DateTime.now(),
    );

    final result = await _addTodo(todo);
    result.fold(
      (failure) => state = state.copyWith(
        status: TodoStatus.error,
        errorMessage: failure.message,
      ),
      (added) => state = state.copyWith(
        status: TodoStatus.loaded,
        todos: [added, ...state.todos],
      ),
    );
  }

  Future<void> toggleTodo(Todo todo) async {
    final updated = todo.copyWith(isCompleted: !todo.isCompleted);
    await _persistUpdate(updated);
  }

  Future<void> editTodo({
    required Todo todo,
    required String title,
    required String description,
  }) async {
    final updated = todo.copyWith(
      title: title.trim(),
      description: description.trim(),
    );
    await _persistUpdate(updated);
  }

  Future<void> _persistUpdate(Todo updated) async {
    final result = await _updateTodo(updated);
    result.fold(
      (failure) => state = state.copyWith(
        status: TodoStatus.error,
        errorMessage: failure.message,
      ),
      (todo) => state = state.copyWith(
        status: TodoStatus.loaded,
        todos: [
          for (final t in state.todos) t.id == todo.id ? todo : t,
        ],
      ),
    );
  }

  Future<void> deleteTodo(String id) async {
    final result = await _deleteTodo(id);
    result.fold(
      (failure) => state = state.copyWith(
        status: TodoStatus.error,
        errorMessage: failure.message,
      ),
      (_) => state = state.copyWith(
        status: TodoStatus.loaded,
        todos: state.todos.where((t) => t.id != id).toList(),
      ),
    );
  }

  void setFilter(TodoFilter filter) {
    state = state.copyWith(filter: filter);
  }
}
