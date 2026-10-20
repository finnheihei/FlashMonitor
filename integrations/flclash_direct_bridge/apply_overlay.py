"""Apply reviewable prototype only to an explicit isolated source copy."""
import argparse
import hashlib
import json
import shutil
from pathlib import Path


def apply(destination):
    destination=destination.resolve()
    if destination.name!='source' or destination.parent.name!='fm-flclash-direct-bridge-20261015':
        raise ValueError('Only the named isolated source copy is allowed')
    if not (destination/'pubspec.yaml').is_file() or not (destination/'core'/'go.mod').is_file():raise ValueError('Not a FlClash source tree')
    overlay=Path(__file__).parent/'overlay';changed=[]
    for source in overlay.rglob('*'):
        if source.is_file():
            target=destination/source.relative_to(overlay);target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target);changed.append(str(source.relative_to(overlay)))
    path=destination/'lib'/'database'/'database.dart';text=path.read_text(encoding='utf-8')
    marker="import '../bridge/journal.dart';"
    if marker not in text:
        text=marker+'\n'+text
        text=text.replace('int get schemaVersion => 3;','int get schemaVersion => 4;')
        text=text.replace('return MigrationStrategy(\n','return MigrationStrategy(\n      onCreate: (m) async { await m.createAll(); await customStatement(bridgeJournalMigration); },\n',1)
        text=text.replace('onUpgrade: (m, from, to) async {','onUpgrade: (m, from, to) async {\n        if (from < 4) await customStatement(bridgeJournalMigration);',1)
        path.write_text(text,encoding='utf-8')
    changed.append('lib/database/database.dart')
    path=destination/'core'/'constant.go';text=path.read_text(encoding='utf-8')
    if 'bridgeStrictCapabilitiesMethod' not in text:
        text=text.replace('\n\tgetConfigMethod','\n\tbridgeStrictCapabilitiesMethod CoreMethod = "bridgeStrictCapabilities"\n\tbridgeStrictPrepareMethod CoreMethod = "bridgeStrictPrepare"\n\tbridgeStrictApplyMethod CoreMethod = "bridgeStrictApply"\n\tgetConfigMethod',1)
        path.write_text(text,encoding='utf-8')
    changed.append('core/constant.go')
    path=destination/'lib'/'core'/'method.dart';text=path.read_text(encoding='utf-8')
    if '  bridgeStrictCapabilities,' not in text:
        text=text.replace('  setupConfig,','  setupConfig,\n  bridgeStrictCapabilities,\n  bridgeStrictPrepare,\n  bridgeStrictApply,',1)
        path.write_text(text,encoding='utf-8')
    changed.append('lib/core/method.dart')
    path=destination/'lib'/'core'/'interface.dart';text=path.read_text(encoding='utf-8')
    if 'Future<Map<String, dynamic>> bridgeStrictCapabilities()' not in text:
        text=text.replace('  Future<String> setupConfig(SetupParams setupParams);','''  Future<String> setupConfig(SetupParams setupParams);
  Future<Map<String, dynamic>> bridgeStrictCapabilities();
  Future<Map<String, dynamic>> bridgeStrictPrepare(String candidate);
  Future<bool> bridgeStrictApply(String ticket);''',1)
        anchor='  @override\n  Future<String> setupConfig(SetupParams setupParams) async {'
        insert='''  @override
  Future<Map<String, dynamic>> bridgeStrictCapabilities() async {
    return await _invokeMethod<Map<String, dynamic>>(method: CoreMethod.bridgeStrictCapabilities) ?? {};
  }

  @override
  Future<Map<String, dynamic>> bridgeStrictPrepare(String candidate) async {
    final result = await _invokeMethod<Map<String, dynamic>>(method: CoreMethod.bridgeStrictPrepare, arguments: {'candidate': candidate});
    if (result == null) throw StateError('strict_prepare_empty');
    return result;
  }

  @override
  Future<bool> bridgeStrictApply(String ticket) async {
    return await _invokeMethod<bool>(method: CoreMethod.bridgeStrictApply, arguments: {'ticket': ticket}) ?? false;
  }

'''
        if anchor not in text:raise ValueError('Pinned CoreHandlerInterface changed')
        text=text.replace(anchor,insert+anchor,1);path.write_text(text,encoding='utf-8')
    changed.append('lib/core/interface.dart')
    path=destination/'lib'/'core'/'controller.dart';text=path.read_text(encoding='utf-8')
    if 'Future<Map<String, dynamic>> bridgeStrictCapabilities()' not in text:
        anchor='  Future<String> setupConfig({'
        insert='''  Future<Map<String, dynamic>> bridgeStrictCapabilities() => _interface.bridgeStrictCapabilities();

  Future<Map<String, dynamic>> bridgeStrictPrepare(String candidate) => _interface.bridgeStrictPrepare(candidate);

  Future<bool> bridgeStrictApply(String ticket) => _interface.bridgeStrictApply(ticket);

'''
        if anchor not in text:raise ValueError('Pinned CoreController changed')
        text=text.replace(anchor,insert+anchor,1);path.write_text(text,encoding='utf-8')
    changed.append('lib/core/controller.dart')
    manifest={name:hashlib.sha256((destination/name).read_bytes()).hexdigest() for name in changed}
    (destination.parent/'overlay-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps({'isolated_source':str(destination),'changed':changed,'production_enabled':False}))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('destination',type=Path)
    apply(parser.parse_args().destination)
