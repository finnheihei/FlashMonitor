import 'dart:convert';

import 'package:crypto/crypto.dart';
import 'package:drift/drift.dart';
import 'package:fl_clash/common/common.dart';
import 'package:fl_clash/database/database.dart';
import 'package:fl_clash/enum/enum.dart';
import 'package:fl_clash/models/models.dart';

const bridgeJournalMigration = '''
CREATE TABLE IF NOT EXISTS monitor_bridge_operations (
 operation_id TEXT NOT NULL PRIMARY KEY,
 profile_id INTEGER NOT NULL,
 state TEXT NOT NULL,
 intent_hash TEXT NOT NULL,
 owned_json TEXT NOT NULL,
 receipt_json TEXT NOT NULL
)''';

class BridgeJournal {
  BridgeJournal(this.database);
  final Database database;

  String fingerprint(Object value) => sha256.convert(utf8.encode(jsonEncode(value))).toString();

  Future<Map<String, dynamic>?> find(String operationId) async {
    final row = await database.customSelect(
      'SELECT * FROM monitor_bridge_operations WHERE operation_id = ?',
      variables: [Variable(operationId)],
    ).getSingleOrNull();
    return row?.data;
  }

  Future<void> setState(String operationId, String state, Map<String, Object?> receipt) => database.customStatement(
    'UPDATE monitor_bridge_operations SET state = ?, receipt_json = ? WHERE operation_id = ?',
    [state, jsonEncode(receipt), operationId],
  );

  Future<List<Map<String, dynamic>>> ownedRows(int ruleId) async => (await database.customSelect(
    'SELECT r.*, m.id AS mapping_id, m.profile_id, m.scene, m."order" FROM rules r JOIN profile_rule_mapping m ON m.rule_id=r.id WHERE r.id=? ORDER BY m.id',
    variables: [Variable(ruleId)],
  ).get()).map((row) => row.data).toList();

  Future<Map<String, Object?>> insert({required String operationId, required int profileId, required String content, required String intentHash, Map<String, Object?> context = const {}}) => database.transaction(() async {
    final existing = await find(operationId);
    if (existing != null) {
      if (existing['intent_hash'] != intentHash || existing['profile_id'] != profileId) throw StateError('operation_scope_conflict');
      return jsonDecode(existing['receipt_json'] as String) as Map<String, dynamic>;
    }
    final effective = await database.rulesDao.queryAddedRules(profileId).get();
    final added = await database.rulesDao.queryProfileAddedRules(profileId).get();
    if (effective.any((rule) => rule.ruleAction == RuleAction.AND && rule.content == content && rule.ruleTarget == 'REJECT')) throw StateError('external_duplicate_not_owned');
    if (added.isNotEmpty && added.first.order == null) throw StateError('unknown_rule_order');
    final order = indexing.generateKeyBetween(null, added.isEmpty ? null : added.first.order);
    if (order == null) throw StateError('unknown_rule_order');
    final rule = Rule(id: snowflake.id, ruleAction: RuleAction.AND, content: content, ruleTarget: 'REJECT', order: order);
    await database.rulesDao.putProfileAddedRule(profileId, rule);
    final rows = await ownedRows(rule.id);
    if (rows.length != 1) throw StateError('ambiguous_owned_row');
    final receipt = <String, Object?>{...context, 'operation_id': operationId, 'profile_id': profileId.toString(), 'rule_id': rule.id.toString(), 'mapping_id': '${profileId}_${rule.id}_added', 'state': 'persisted'};
    await database.customStatement(
      'INSERT INTO monitor_bridge_operations VALUES (?, ?, ?, ?, ?, ?)',
      [operationId, profileId, 'persisted', intentHash, jsonEncode(rows), jsonEncode(receipt)],
    );
    return receipt;
  });

  Future<void> removeOwned(String operationId) => database.transaction(() async {
    final operation = await find(operationId);
    if (operation == null) throw StateError('operation_not_found');
    final receipt = jsonDecode(operation['receipt_json'] as String) as Map<String, dynamic>;
    final ruleId = int.parse(receipt['rule_id'] as String);
    final current = await ownedRows(ruleId);
    final expected = jsonDecode(operation['owned_json'] as String);
    if (current.length != 1 || fingerprint(current) != fingerprint(expected)) throw StateError('ownership_or_external_reference_conflict');
    await database.customUpdate(
      'DELETE FROM profile_rule_mapping WHERE id=? AND profile_id=? AND rule_id=? AND scene=?',
      variables: [Variable(receipt['mapping_id'] as String), Variable(operation['profile_id'] as int), Variable(ruleId), const Variable('added')],
      updates: {database.profileRuleLinks},
    );
    await (database.delete(database.rules)..where((row) => row.id.equals(ruleId))).go();
    await setState(operationId, 'durable_removed_runtime_pending', receipt);
  });
}
