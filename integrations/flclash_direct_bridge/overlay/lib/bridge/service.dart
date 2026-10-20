import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:crypto/crypto.dart';

import 'coordinator.dart';
import 'journal.dart';

class BridgeContext {
  const BridgeContext({required this.profileId, required this.revision, required this.instanceId, required this.rootIdentity, required this.mode, required this.overwriteType});
  final String profileId;
  final String revision;
  final String instanceId;
  final String rootIdentity;
  final String mode;
  final String overwriteType;
}

class BridgeCandidate {
  const BridgeCandidate({required this.context, required this.content, required this.ruleHash, required this.yaml, required this.nonrulesHash, required this.unmanagedHash});
  final BridgeContext context;
  final String content;
  final String ruleHash;
  final String yaml;
  final String nonrulesHash;
  final String unmanagedHash;
}

class RejectedBeforeApply implements Exception {}

String bridgeErrorCode(Object error) => error is StateError && RegExp(r'^[a-z_]+$').hasMatch(error.message) ? error.message : 'native_operation_failed';

abstract interface class NativeBridgeHooks {
  bool get strictLoadPreservesOld;
  bool get parserSideEffectsAudited;
  bool get clientLifecycleTested;
  Future<BridgeContext> snapshot();
  Future<BridgeCandidate> preview(List<Map<String, dynamic>> subjects);
  Future<String> validateFully(BridgeCandidate candidate);
  Future<BridgeCandidate> invalidateAndCompileCurrent();
  Future<void> loadStrict(BridgeCandidate candidate, String validationTicket);
  Future<Map<String, Object?>> orderedReceipt(BridgeCandidate candidate);
  Future<Map<String, Object?>> auditOperation(Map<String, dynamic> receipt);
}

class _Prepared {
  _Prepared(this.operationId, this.candidate, this.token);
  final String operationId;
  final BridgeCandidate candidate;
  final String token;
  final DateTime expires = DateTime.now().add(const Duration(minutes: 2));
}

class NativeBridgeService {
  NativeBridgeService(this.coordinator, this.journal, this.hooks);
  final ConfigurationCoordinator coordinator;
  final BridgeJournal journal;
  final NativeBridgeHooks hooks;
  final Map<String, _Prepared> _prepared = {};

  String _nonce() => List.generate(32, (_) => Random.secure().nextInt(256).toRadixString(16).padLeft(2, '0')).join();
  String _hash(String text) => sha256.convert(utf8.encode(text)).toString();

  Future<Map<String, Object?>> capabilities() async {
    final context = await hooks.snapshot();
    return {'protocol': 1, 'schema_version': journal.database.schemaVersion, 'instance_id': context.instanceId,
      'root_identity': context.rootIdentity, 'profile_id': context.profileId, 'revision': context.revision,
      'mode': context.mode, 'profile_mode': context.overwriteType, 'native_transaction': true,
      'coordinated_mutations': coordinator.fullyCoordinated, 'strict_load_preserves_old': hooks.strictLoadPreservesOld,
      'parser_side_effects_audited': hooks.parserSideEffectsAudited, 'client_lifecycle_tested': hooks.clientLifecycleTested,
      'ordered_receipts': true};
  }

  Future<void> _requireReady() async {
    if (!coordinator.fullyCoordinated || !hooks.strictLoadPreservesOld || !hooks.parserSideEffectsAudited || !hooks.clientLifecycleTested || journal.database.schemaVersion != 4) {
      throw StateError('client_acceptance_incomplete');
    }
    final context = await hooks.snapshot();
    if (context.mode != 'rule' || context.overwriteType != 'standard') throw StateError('unsupported_profile');
  }

  Future<Map<String, Object?>> prepare(Map<String, dynamic> request) => coordinator.exclusive(() async {
    await _requireReady();
    final operation = request['operation_id'] as String;
    if (!RegExp(r'^[a-f0-9]{32}$').hasMatch(operation)) throw StateError('invalid_operation');
    final context = await hooks.snapshot();
    if (request['profile_id'] != context.profileId || request['expected_revision'] != context.revision) throw StateError('context_changed');
    final values = request['subjects'];
    if (values is! List || values.isEmpty || values.length > 32) throw StateError('invalid_scope');
    final subjects = values.map((item) => Map<String, dynamic>.from(item as Map)).toList();
    for (final subject in subjects) {
      if (subject.keys.toSet().difference({'kind', 'value', 'include_subdomains'}).isNotEmpty || subject.length != 3) throw StateError('invalid_subject_fields');
      final kind = subject['kind'];
      final value = subject['value'];
      if (!{'process_path', 'process_name', 'domain'}.contains(kind) || value is! String || value.isEmpty || value.length > 1024 || RegExp(r'[,\x00-\x1f\x7f]').hasMatch(value) || subject['include_subdomains'] is! bool) throw StateError('invalid_subject');
      if (kind != 'domain' && (subject['include_subdomains'] == true || !value.toLowerCase().endsWith('.exe'))) throw StateError('invalid_process_scope');
    }
    _prepared.removeWhere((_, value) => DateTime.now().isAfter(value.expires));
    if (_prepared.length >= 64) throw StateError('prepare_budget_exceeded');
    final candidate = await hooks.preview(subjects);
    if (candidate.context.revision != context.revision || candidate.context.profileId != context.profileId) throw StateError('preview_context_changed');
    final prepared = _Prepared(operation, candidate, _nonce());
    _prepared[operation] = prepared;
    return {'operation_id': operation, 'profile_id': context.profileId, 'revision': context.revision, 'rule_hash': candidate.ruleHash, 'token': prepared.token};
  });

  Future<Map<String, Object?>> status(String operationId) async {
    final row = await journal.find(operationId);
    if (row == null) throw StateError('operation_not_found');
    final receipt = jsonDecode(row['receipt_json'] as String) as Map<String, dynamic>;
    if (row['state'] != 'verified') return {...receipt, 'state': row['state']};
    final context = await hooks.snapshot();
    final owned = await journal.ownedRows(int.parse(receipt['rule_id'] as String));
    if (context.profileId != row['profile_id'].toString() || context.rootIdentity != receipt['root_identity'] || journal.fingerprint(owned) != journal.fingerprint(jsonDecode(row['owned_json'] as String))) {
      return {...receipt, 'state': 'recovery_required'};
    }
    final audit = await hooks.auditOperation(receipt);
    if (audit['ordered_verified'] != true || audit['runtime_rule_hash'] != row['intent_hash'] || audit['persistent_rule_hash'] != row['intent_hash']) return {...receipt, 'state': 'recovery_required'};
    return {...receipt, ...audit, 'state': 'verified'};
  }

  Future<Map<String, Object?>> commit(Map<String, dynamic> request) => coordinator.exclusive(() async {
    await _requireReady();
    final operation = request['operation_id'] as String;
    final existing = await journal.find(operation);
    if (existing != null) return status(operation);
    final prepared = _prepared[operation];
    if (prepared == null || request['token'] != prepared.token || DateTime.now().isAfter(prepared.expires)) throw StateError('token_expired');
    final candidate = prepared.candidate;
    final context = await hooks.snapshot();
    if (request['expected_revision'] != context.revision || context.revision != candidate.context.revision || context.profileId != candidate.context.profileId || context.instanceId != candidate.context.instanceId || context.rootIdentity != candidate.context.rootIdentity) throw StateError('context_changed');
    final ticket = await hooks.validateFully(candidate);
    var receipt = await journal.insert(operationId: operation, profileId: int.parse(context.profileId), content: candidate.content, intentHash: candidate.ruleHash,
      context: {'instance_id': context.instanceId, 'root_identity': context.rootIdentity, 'context_revision': context.revision});
    var loadSent = false;
    try {
      final compiled = await hooks.invalidateAndCompileCurrent();
      if (_hash(compiled.yaml) != _hash(candidate.yaml) || compiled.nonrulesHash != candidate.nonrulesHash || compiled.unmanagedHash != candidate.unmanagedHash) throw StateError('compiled_candidate_changed');
      await journal.setState(operation, 'applying', receipt);
      loadSent = true;
      await hooks.loadStrict(compiled, ticket);
      final verified = await hooks.orderedReceipt(compiled);
      receipt = {...receipt, ...verified, 'operation_id': operation, 'profile_id': context.profileId, 'instance_id': context.instanceId,
        'state': 'verified', 'persistent_rule_hash': candidate.ruleHash, 'compiled_rule_hash': candidate.ruleHash,
        'nonrules_before': candidate.nonrulesHash, 'nonrules_after': compiled.nonrulesHash,
        'unmanaged_before': candidate.unmanagedHash, 'unmanaged_after': compiled.unmanagedHash};
      if (verified['ordered_verified'] != true || verified['runtime_rule_hash'] != candidate.ruleHash) throw StateError('runtime_not_verified');
      await journal.setState(operation, 'verified', receipt);
      coordinator.revision++;
      return receipt;
    } catch (error) {
      if (loadSent && error is! RejectedBeforeApply || error is TimeoutException) {
        await journal.setState(operation, 'unknown', {...receipt, 'error': 'apply_outcome_unknown'});
        rethrow;
      }
      try {
        await journal.removeOwned(operation);
        final remaining = await hooks.invalidateAndCompileCurrent();
        final undoTicket = await hooks.validateFully(remaining);
        await hooks.loadStrict(remaining, undoTicket);
        final restored = await hooks.orderedReceipt(remaining);
        if (restored['ordered_verified'] != true || restored['owned_absent'] != true) throw StateError('compensation_not_verified');
        await journal.setState(operation, 'rolled_back', {...receipt, ...restored});
      } catch (recovery) {
        await journal.setState(operation, 'recovery_required', {...receipt, 'error': bridgeErrorCode(recovery)});
      }
      rethrow;
    }
  });

  Future<Map<String, Object?>> undo(String operation) => coordinator.exclusive(() async {
    await _requireReady();
    final row = await journal.find(operation);
    if (row == null) throw StateError('operation_not_found');
    if (row['state'] == 'revoked') return status(operation);
    final context = await hooks.snapshot();
    if (row['profile_id'].toString() != context.profileId) throw StateError('profile_changed');
    final old = jsonDecode(row['receipt_json'] as String) as Map<String, dynamic>;
    if (old['instance_id'] != context.instanceId) throw StateError('instance_changed_recovery_required');
    if (!{'verified', 'durable_removed_runtime_pending'}.contains(row['state'])) throw StateError('unknown_outcome_recovery_required');
    if (row['state'] == 'verified') await journal.removeOwned(operation);
    final remaining = await hooks.invalidateAndCompileCurrent();
    final ticket = await hooks.validateFully(remaining);
    await hooks.loadStrict(remaining, ticket);
    final check = await hooks.orderedReceipt(remaining);
    if (check['ordered_verified'] != true || check['owned_absent'] != true) throw StateError('undo_not_verified');
    final receipt = <String, Object?>{...old, ...check, 'state': 'revoked', 'operation_id': operation, 'profile_id': context.profileId,
      'unmanaged_before': remaining.unmanagedHash, 'unmanaged_after': remaining.unmanagedHash};
    await journal.setState(operation, 'revoked', receipt);
    coordinator.revision++;
    return receipt;
  });
}
