import 'package:dartz/dartz.dart';

import '../../../../core/error/failure.dart';
import '../entities/todo.dart';

/// Contract for todo persistence, defined by the domain layer.
///
/// The data layer provides the implementation. Methods return
/// `Either<Failure, T>` so callers handle errors without try/catch.
abstract class TodoRepository {
  Future<Either<Failure, List<Todo>>> getTodos();

  Future<Either<Failure, Todo>> addTodo(Todo todo);

  Future<Either<Failure, Todo>> updateTodo(Todo todo);

  Future<Either<Failure, Unit>> deleteTodo(String id);
}
