import 'package:drift/native.dart';
import 'package:fl_clash/bridge/journal.dart';
import 'package:fl_clash/database/database.dart';
import 'package:fl_clash/models/models.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  late Database database;
  late BridgeJournal journal;
  setUp(() async {
    database = Database(NativeDatabase.memory());
    journal = BridgeJournal(database);
    await database.customStatement('INSERT INTO profiles (id,label,url,overwrite_type,auto_update_duration_millis,auto_update,selected_map,unfold_set) VALUES (123,?,?,?,?,?,?,?)', ['fixture', '', 'standard', 0, 0, '{}', '[]']);
  });
  tearDown(() => database.close());

  test('native rule and ownership journal share one transaction and canonical key', () async {
    final receipt = await journal.insert(operationId: 'a' * 32, profileId: 123, content: '((PROCESS-NAME,fixture.exe),(IP-CIDR6,::/0,no-resolve))', intentHash: 'scope');
    final id = receipt['rule_id']! as String;
    expect(receipt['mapping_id'], '123_${id}_added');
    expect((await database.rulesDao.queryAddedRules(123).get()).length, 1);
    expect((await journal.find('a' * 32))!['state'], 'persisted');
    final duplicate = await journal.insert(operationId: 'a' * 32, profileId: 123, content: '((PROCESS-NAME,fixture.exe),(IP-CIDR6,::/0,no-resolve))', intentHash: 'scope');
    expect(duplicate['rule_id'], id);
  });

  test('external reference prevents precise undo', () async {
    final receipt = await journal.insert(operationId: 'b' * 32, profileId: 123, content: '((DOMAIN,fixture.example),(IP-CIDR6,::/0,no-resolve))', intentHash: 'scope');
    final id = int.parse(receipt['rule_id']! as String);
    await database.customStatement('INSERT INTO profile_rule_mapping (id,rule_id) VALUES (?,?)', [id.toString(), id]);
    await expectLater(journal.removeOwned('b' * 32), throwsStateError);
    expect((await journal.ownedRows(id)).length, 2);
  });

  test('undo removes only owned row and retains unrelated native rule', () async {
    await database.rulesDao.putProfileAddedRule(123, Rule.parse('DOMAIN,original.example,DIRECT').copyWith(order: 'a0'));
    await journal.insert(operationId: 'c' * 32, profileId: 123, content: '((DOMAIN,fixture.example),(IP-CIDR6,::/0,no-resolve))', intentHash: 'scope');
    await journal.removeOwned('c' * 32);
    final remaining = await database.rulesDao.queryAddedRules(123).get();
    expect(remaining.single.content, 'original.example');
    expect((await journal.find('c' * 32))!['state'], 'durable_removed_runtime_pending');
  });
}
