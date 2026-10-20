import argparse
import difflib
import json
from pathlib import Path


def export(original,modified,output):
    manifest=json.loads((modified.parent/'overlay-manifest.json').read_text(encoding='utf-8'))
    patch=[]
    for relative in sorted(manifest):
        name=relative.replace('\\','/');old=original/name;new=modified/name
        before=old.read_text(encoding='utf-8').splitlines(keepends=True) if old.exists() else []
        after=new.read_text(encoding='utf-8').splitlines(keepends=True)
        patch.extend(difflib.unified_diff(before,after,fromfile='a/'+name if old.exists() else '/dev/null',tofile='b/'+name))
    output.write_text(''.join(patch),encoding='utf-8')
    print(json.dumps({'patch':str(output),'files':len(manifest),'new_client_built':False}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('original',type=Path);parser.add_argument('modified',type=Path);parser.add_argument('output',type=Path)
    args=parser.parse_args();export(args.original,args.modified,args.output)
