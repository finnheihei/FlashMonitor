import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'service.dart';

class BridgeSessionServer {
  BridgeSessionServer(this.service);
  final NativeBridgeService service;
  HttpServer? _server;
  final String token = List.generate(32, (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0')).join();

  Future<Uri> startForVerifiedBootstrap() async {
    if (_server != null) throw StateError('session_already_started');
    _server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0, shared: false);
    _server!.listen(_handle);
    return Uri.parse('http://127.0.0.1:${_server!.port}/bridge');
  }

  Future<void> close() async {
    await _server?.close(force: true);
    _server = null;
  }

  Future<void> _handle(HttpRequest request) async {
    request.response.headers.contentType = ContentType.json;
    request.response.headers.set('Cache-Control', 'no-store');
    try {
      if (request.uri.path != '/bridge' || request.uri.hasQuery || request.method != 'POST' || request.headers.value('Origin') != null || request.headers.value('Authorization') != 'Bearer $token') throw StateError('unauthorized');
      if (request.headers.contentType?.mimeType != 'application/json') throw StateError('invalid_content_type');
      final bytes = <int>[];
      await for (final chunk in request.timeout(const Duration(seconds: 5))) {
        bytes.addAll(chunk);
        if (bytes.length > 65536) throw StateError('request_too_large');
      }
      final body = Map<String, dynamic>.from(jsonDecode(utf8.decode(bytes)) as Map);
      if (body.keys.toSet().difference({'action', 'params'}).isNotEmpty || body.length != 2) throw StateError('unknown_fields');
      final action = body['action'];
      final params = Map<String, dynamic>.from(body['params'] as Map);
      final allowed = <String, Set<String>>{'capabilities': {}, 'status': {'operation_id'}, 'prepare': {'profile_id', 'subjects', 'expected_revision', 'operation_id'}, 'commit': {'operation_id', 'token', 'expected_revision'}, 'undo': {'operation_id'}};
      if (!allowed.containsKey(action) || params.keys.toSet().difference(allowed[action]!).isNotEmpty || params.length != allowed[action]!.length) throw StateError('invalid_action_fields');
      final result = switch (action) {
        'capabilities' => await service.capabilities(),
        'prepare' => await service.prepare(params),
        'commit' => await service.commit(params),
        'status' => await service.status(params['operation_id'] as String),
        'undo' => await service.undo(params['operation_id'] as String),
        _ => throw StateError('invalid_action'),
      };
      request.response.write(jsonEncode(result));
    } catch (error) {
      request.response.statusCode = HttpStatus.conflict;
      request.response.write(jsonEncode({'error': bridgeErrorCode(error)}));
    } finally {
      await request.response.close();
    }
  }
}
