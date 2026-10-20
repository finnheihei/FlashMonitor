import 'dart:async';

enum ConfigurationWriter { ruleEdit, profileSwitch, subscriptionPublish, sourceRestore, configLoad }

class ConfigurationCoordinator {
  final Set<ConfigurationWriter> enrolled = {};
  final Object _zoneKey = Object();
  Future<void> _tail = Future<void>.value();
  int revision = 0;

  bool get fullyCoordinated => enrolled.length == ConfigurationWriter.values.length;

  Future<T> exclusive<T>(Future<T> Function() action) async {
    if (Zone.current[_zoneKey] == this) return action();
    final previous = _tail;
    final done = Completer<void>();
    _tail = done.future;
    await previous;
    try {
      return await runZoned(action, zoneValues: {_zoneKey: this});
    } finally {
      done.complete();
    }
  }

  Future<T> mutation<T>(ConfigurationWriter writer, Future<T> Function() action) => exclusive(() async {
    if (!enrolled.contains(writer)) throw StateError('writer_not_coordinated');
    final result = await action();
    revision++;
    return result;
  });
}
