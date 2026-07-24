import 'package:dartz/dartz.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:todo_app/core/error/failure.dart';
import 'package:todo_app/features/todo/domain/entities/todo.dart';
import 'package:todo_app/features/todo/domain/repositories/todo_repository.dart';
import 'package:todo_app/features/todo/presentation/pages/todo_list_page.dart';
import 'package:todo_app/features/todo/presentation/providers/todo_providers.dart';

/// In-memory [TodoRepository] so widget tests don't touch sqflite.
class FakeTodoRepository implements TodoRepository {
  FakeTodoRepository(this._todos);

  final List<Todo> _todos;

  @override
  Future<Either<Failure, List<Todo>>> getTodos() async => Right(_todos);

  @override
  Future<Either<Failure, Todo>> addTodo(Todo todo) async {
    _todos.insert(0, todo);
    return Right(todo);
  }

  @override
  Future<Either<Failure, Todo>> updateTodo(Todo todo) async {
    final i = _todos.indexWhere((t) => t.id == todo.id);
    if (i != -1) _todos[i] = todo;
    return Right(todo);
  }

  @override
  Future<Either<Failure, Unit>> deleteTodo(String id) async {
    _todos.removeWhere((t) => t.id == id);
    return const Right(unit);
  }
}

void main() {
  testWidgets('renders a seeded todo', (tester) async {
    final todo = Todo(
      id: '1',
      title: 'Buy milk',
      createdAt: DateTime(2026, 7, 24),
    );

    await tester.pumpWidget(
      ProviderScope(
        overrides: [
          todoRepositoryProvider.overrideWithValue(
            FakeTodoRepository([todo]),
          ),
        ],
        child: const MaterialApp(home: TodoListPage()),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('Buy milk'), findsOneWidget);
    expect(find.text('My Tasks'), findsOneWidget);
  });
}
