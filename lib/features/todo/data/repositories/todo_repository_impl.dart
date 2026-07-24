import 'package:dartz/dartz.dart';

import '../../../../core/error/exceptions.dart';
import '../../../../core/error/failure.dart';
import '../../domain/entities/todo.dart';
import '../../domain/repositories/todo_repository.dart';
import '../datasources/todo_local_datasource.dart';
import '../models/todo_model.dart';

/// Concrete [TodoRepository] backed by a local sqflite data source.
///
/// Converts thrown [DatabaseException]s from the data source into typed
/// [Failure]s so the rest of the app never deals with raw exceptions.
class TodoRepositoryImpl implements TodoRepository {
  TodoRepositoryImpl(this._localDataSource);

  final TodoLocalDataSource _localDataSource;

  @override
  Future<Either<Failure, List<Todo>>> getTodos() async {
    try {
      final todos = await _localDataSource.getTodos();
      return Right(todos);
    } on DatabaseException catch (e) {
      return Left(DatabaseFailure(e.message));
    } catch (e) {
      return Left(UnexpectedFailure(e.toString()));
    }
  }

  @override
  Future<Either<Failure, Todo>> addTodo(Todo todo) async {
    try {
      final result = await _localDataSource.addTodo(
        TodoModel.fromEntity(todo),
      );
      return Right(result);
    } on DatabaseException catch (e) {
      return Left(DatabaseFailure(e.message));
    } catch (e) {
      return Left(UnexpectedFailure(e.toString()));
    }
  }

  @override
  Future<Either<Failure, Todo>> updateTodo(Todo todo) async {
    try {
      final result = await _localDataSource.updateTodo(
        TodoModel.fromEntity(todo),
      );
      return Right(result);
    } on DatabaseException catch (e) {
      return Left(DatabaseFailure(e.message));
    } catch (e) {
      return Left(UnexpectedFailure(e.toString()));
    }
  }

  @override
  Future<Either<Failure, Unit>> deleteTodo(String id) async {
    try {
      await _localDataSource.deleteTodo(id);
      return const Right(unit);
    } on DatabaseException catch (e) {
      return Left(DatabaseFailure(e.message));
    } catch (e) {
      return Left(UnexpectedFailure(e.toString()));
    }
  }
}
