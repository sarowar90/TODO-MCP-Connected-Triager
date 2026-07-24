import 'package:dartz/dartz.dart';

import '../../../../core/error/failure.dart';
import '../entities/todo.dart';
import '../repositories/todo_repository.dart';

/// Updates an existing todo (title, description, or completion state).
class UpdateTodo {
  const UpdateTodo(this._repository);

  final TodoRepository _repository;

  Future<Either<Failure, Todo>> call(Todo todo) =>
      _repository.updateTodo(todo);
}
