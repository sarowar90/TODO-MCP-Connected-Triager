import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../domain/entities/todo.dart';
import '../providers/todo_list_state.dart';
import '../providers/todo_providers.dart';
import '../widgets/todo_editor_sheet.dart';
import '../widgets/todo_item.dart';

class TodoListPage extends ConsumerWidget {
  const TodoListPage({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final state = ref.watch(todoListNotifierProvider);
    final notifier = ref.read(todoListNotifierProvider.notifier);

    // Surface errors as a snackbar without rebuilding the whole tree on them.
    ref.listen(todoListNotifierProvider, (previous, next) {
      if (next.status == TodoStatus.error && next.errorMessage != null) {
        ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(SnackBar(content: Text(next.errorMessage!)));
      }
    });

    return Scaffold(
      appBar: AppBar(
        title: const Text('My Tasks'),
        bottom: PreferredSize(
          preferredSize: const Size.fromHeight(56),
          child: _FilterBar(
            current: state.filter,
            activeCount: state.activeCount,
            onChanged: notifier.setFilter,
          ),
        ),
      ),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => _openEditor(context, ref),
        icon: const Icon(Icons.add),
        label: const Text('Add'),
      ),
      body: _Body(state: state),
    );
  }

  Future<void> _openEditor(
    BuildContext context,
    WidgetRef ref, {
    Todo? todo,
  }) async {
    final result = await TodoEditorSheet.show(context, todo: todo);
    if (result == null) return;

    final notifier = ref.read(todoListNotifierProvider.notifier);
    if (todo == null) {
      await notifier.addTodo(
        title: result.title,
        description: result.description,
      );
    } else {
      await notifier.editTodo(
        todo: todo,
        title: result.title,
        description: result.description,
      );
    }
  }
}

class _Body extends ConsumerWidget {
  const _Body({required this.state});

  final TodoListState state;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    if (state.status == TodoStatus.loading && state.todos.isEmpty) {
      return const Center(child: CircularProgressIndicator());
    }

    final todos = state.visibleTodos;
    if (todos.isEmpty) {
      return _EmptyState(filter: state.filter);
    }

    final notifier = ref.read(todoListNotifierProvider.notifier);
    return ListView.separated(
      padding: const EdgeInsets.only(bottom: 88),
      itemCount: todos.length,
      separatorBuilder: (_, _) => const Divider(height: 1),
      itemBuilder: (context, index) {
        final todo = todos[index];
        return TodoItem(
          todo: todo,
          onToggle: (_) => notifier.toggleTodo(todo),
          onDelete: () => notifier.deleteTodo(todo.id),
          onTap: () async {
            final result = await TodoEditorSheet.show(context, todo: todo);
            if (result == null) return;
            await notifier.editTodo(
              todo: todo,
              title: result.title,
              description: result.description,
            );
          },
        );
      },
    );
  }
}

class _FilterBar extends StatelessWidget {
  const _FilterBar({
    required this.current,
    required this.activeCount,
    required this.onChanged,
  });

  final TodoFilter current;
  final int activeCount;
  final ValueChanged<TodoFilter> onChanged;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.fromLTRB(12, 0, 12, 8),
      child: Row(
        children: [
          Expanded(
            child: SegmentedButton<TodoFilter>(
              segments: const [
                ButtonSegment(
                  value: TodoFilter.all,
                  label: Text('All'),
                ),
                ButtonSegment(
                  value: TodoFilter.active,
                  label: Text('Active'),
                ),
                ButtonSegment(
                  value: TodoFilter.completed,
                  label: Text('Done'),
                ),
              ],
              selected: {current},
              onSelectionChanged: (selection) => onChanged(selection.first),
            ),
          ),
          const SizedBox(width: 12),
          Text(
            '$activeCount left',
            style: Theme.of(context).textTheme.labelLarge,
          ),
        ],
      ),
    );
  }
}

class _EmptyState extends StatelessWidget {
  const _EmptyState({required this.filter});

  final TodoFilter filter;

  @override
  Widget build(BuildContext context) {
    final (icon, message) = switch (filter) {
      TodoFilter.active => (
          Icons.check_circle_outline,
          'No active tasks. Nice work!',
        ),
      TodoFilter.completed => (
          Icons.inbox_outlined,
          'Nothing completed yet.',
        ),
      TodoFilter.all => (
          Icons.task_alt,
          'No tasks yet.\nTap “Add” to create one.',
        ),
    };

    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 64, color: Theme.of(context).disabledColor),
          const SizedBox(height: 16),
          Text(
            message,
            textAlign: TextAlign.center,
            style: Theme.of(context).textTheme.titleMedium,
          ),
        ],
      ),
    );
  }
}
