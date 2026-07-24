import 'package:dartz/dartz.dart';

import '../../../../core/error/failure.dart';
import '../repositories/todo_repository.dart';

/// Deletes the todo with the given [id].
class DeleteTodo {
  const DeleteTodo(this._repository);

  final TodoRepository _repository;

  Future<Either<Failure, Unit>> call(String id) =>
      _repository.deleteTodo(id);
}
