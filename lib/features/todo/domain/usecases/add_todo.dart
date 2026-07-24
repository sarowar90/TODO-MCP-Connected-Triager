import 'package:dartz/dartz.dart';

import '../../../../core/error/failure.dart';
import '../entities/todo.dart';
import '../repositories/todo_repository.dart';

/// Persists a new todo.
class AddTodo {
  const AddTodo(this._repository);

  final TodoRepository _repository;

  Future<Either<Failure, Todo>> call(Todo todo) => _repository.addTodo(todo);
}
