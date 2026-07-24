import 'package:sqflite/sqflite.dart' hide DatabaseException;

import '../../../../core/database/app_database.dart';
import '../../../../core/error/exceptions.dart';
import '../models/todo_model.dart';

/// Handles the raw sqflite CRUD operations for todos.
abstract class TodoLocalDataSource {
  Future<List<TodoModel>> getTodos();

  Future<TodoModel> addTodo(TodoModel todo);

  Future<TodoModel> updateTodo(TodoModel todo);

  Future<void> deleteTodo(String id);
}

class TodoLocalDataSourceImpl implements TodoLocalDataSource {
  TodoLocalDataSourceImpl(this._appDatabase);

  final AppDatabase _appDatabase;

  @override
  Future<List<TodoModel>> getTodos() async {
    try {
      final db = await _appDatabase.database;
      final rows = await db.query(
        AppDatabase.todoTable,
        orderBy: 'created_at DESC',
      );
      return rows.map(TodoModel.fromMap).toList();
    } catch (e) {
      throw DatabaseException('Failed to load todos: $e');
    }
  }

  @override
  Future<TodoModel> addTodo(TodoModel todo) async {
    try {
      final db = await _appDatabase.database;
      await db.insert(
        AppDatabase.todoTable,
        todo.toMap(),
        conflictAlgorithm: ConflictAlgorithm.replace,
      );
      return todo;
    } catch (e) {
      throw DatabaseException('Failed to add todo: $e');
    }
  }

  @override
  Future<TodoModel> updateTodo(TodoModel todo) async {
    try {
      final db = await _appDatabase.database;
      final count = await db.update(
        AppDatabase.todoTable,
        todo.toMap(),
        where: 'id = ?',
        whereArgs: [todo.id],
      );
      if (count == 0) {
        throw DatabaseException('Todo with id ${todo.id} not found.');
      }
      return todo;
    } on DatabaseException {
      rethrow;
    } catch (e) {
      throw DatabaseException('Failed to update todo: $e');
    }
  }

  @override
  Future<void> deleteTodo(String id) async {
    try {
      final db = await _appDatabase.database;
      await db.delete(
        AppDatabase.todoTable,
        where: 'id = ?',
        whereArgs: [id],
      );
    } catch (e) {
      throw DatabaseException('Failed to delete todo: $e');
    }
  }
}
