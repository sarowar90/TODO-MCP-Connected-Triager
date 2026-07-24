import 'package:dartz/dartz.dart';

import '../../../../core/error/failure.dart';
import '../entities/todo.dart';
import '../repositories/todo_repository.dart';

/// Returns all todos, newest first.
class GetTodos {
  const GetTodos(this._repository);

  final TodoRepository _repository;

  Future<Either<Failure, List<Todo>>> call() => _repository.getTodos();
}
