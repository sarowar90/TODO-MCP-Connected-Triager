import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../../domain/entities/todo.dart';

/// A single row in the todo list, with a completion checkbox, title/description
/// and swipe-to-delete.
class TodoItem extends StatelessWidget {
  const TodoItem({
    super.key,
    required this.todo,
    required this.onToggle,
    required this.onDelete,
    required this.onTap,
  });

  final Todo todo;
  final ValueChanged<bool?> onToggle;
  final VoidCallback onDelete;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final hasDescription = todo.description.isNotEmpty;

    return Dismissible(
      key: ValueKey(todo.id),
      direction: DismissDirection.endToStart,
      background: Container(
        alignment: Alignment.centerRight,
        padding: const EdgeInsets.symmetric(horizontal: 20),
        color: theme.colorScheme.errorContainer,
        child: Icon(
          Icons.delete_outline,
          color: theme.colorScheme.onErrorContainer,
        ),
      ),
      onDismissed: (_) => onDelete(),
      child: ListTile(
        onTap: onTap,
        leading: Checkbox(
          value: todo.isCompleted,
          onChanged: onToggle,
        ),
        title: Text(
          todo.title,
          style: TextStyle(
            decoration:
                todo.isCompleted ? TextDecoration.lineThrough : null,
            color: todo.isCompleted
                ? theme.disabledColor
                : theme.colorScheme.onSurface,
          ),
        ),
        subtitle: hasDescription
            ? Text(
                todo.description,
                maxLines: 2,
                overflow: TextOverflow.ellipsis,
              )
            : null,
        trailing: Text(
          DateFormat.MMMd().format(todo.createdAt),
          style: theme.textTheme.bodySmall,
        ),
      ),
    );
  }
}
