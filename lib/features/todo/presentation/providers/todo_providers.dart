import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../../core/database/app_database.dart';
import '../../data/datasources/todo_local_datasource.dart';
import '../../data/repositories/todo_repository_impl.dart';
import '../../domain/repositories/todo_repository.dart';
import '../../domain/usecases/add_todo.dart';
import '../../domain/usecases/delete_todo.dart';
import '../../domain/usecases/get_todos.dart';
import '../../domain/usecases/update_todo.dart';
import 'todo_list_notifier.dart';
import 'todo_list_state.dart';

// ---------------------------------------------------------------------------
// Infrastructure
// ---------------------------------------------------------------------------

final appDatabaseProvider = Provider<AppDatabase>((ref) {
  return AppDatabase.instance;
});

final todoLocalDataSourceProvider = Provider<TodoLocalDataSource>((ref) {
  return TodoLocalDataSourceImpl(ref.watch(appDatabaseProvider));
});

final todoRepositoryProvider = Provider<TodoRepository>((ref) {
  return TodoRepositoryImpl(ref.watch(todoLocalDataSourceProvider));
});

// ---------------------------------------------------------------------------
// Use cases
// ---------------------------------------------------------------------------

final getTodosProvider = Provider<GetTodos>((ref) {
  return GetTodos(ref.watch(todoRepositoryProvider));
});

final addTodoProvider = Provider<AddTodo>((ref) {
  return AddTodo(ref.watch(todoRepositoryProvider));
});

final updateTodoProvider = Provider<UpdateTodo>((ref) {
  return UpdateTodo(ref.watch(todoRepositoryProvider));
});

final deleteTodoProvider = Provider<DeleteTodo>((ref) {
  return DeleteTodo(ref.watch(todoRepositoryProvider));
});

// ---------------------------------------------------------------------------
// Presentation state
// ---------------------------------------------------------------------------

final todoListNotifierProvider =
    StateNotifierProvider<TodoListNotifier, TodoListState>((ref) {
  return TodoListNotifier(
    getTodos: ref.watch(getTodosProvider),
    addTodo: ref.watch(addTodoProvider),
    updateTodo: ref.watch(updateTodoProvider),
    deleteTodo: ref.watch(deleteTodoProvider),
  )..loadTodos();
});
