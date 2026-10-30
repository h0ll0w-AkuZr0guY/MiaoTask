"""Generate a static Pages resource directory; never create a GitHub Release."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend.core.plugins.contracts import OFFICIAL_REPOSITORY, branch_name
from scripts.package_activities import build_packages
from backend.core.plugins.resource_mirror import branch_key


def build_site(root, output, source_ref='main'):
    root, output = Path(root), Path(output)
    source_ref = branch_name(source_ref)
    revision = subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip()
    catalog = build_packages(root,output/'packages','activity-resources')
    for entry in catalog['packages']:
        entry.update(source='github-source',source_ref=source_ref,revision=revision[:12],
                     archive='packages/'+entry['url'].rsplit('/',1)[-1])
        entry.pop('url',None)
    catalog.update(source='github-source', source_ref=source_ref,source_commit=revision,revision=revision[:12],transport='pages',warnings=[])
    for relative in ('branches/'+branch_key(source_ref)+'.json','commits/'+revision+'/'+branch_key(source_ref)+'.json'):
        path=output/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf-8')
    previous = json.loads((output/'branches.json').read_text(encoding='utf-8')).get('branches',[]) if (output/'branches.json').is_file() else []
    branches = sorted({branch_name(name) for name in [*previous,source_ref]})
    (output/'branches.json').write_text(json.dumps({'schema':1,'branches':branches}),encoding='utf-8')
    (output/'index.html').write_text('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>MiaoTask 活动资源</title><h1>MiaoTask 活动资源</h1><p>客户端通过固定提交与摘要下载；活动更新不创建框架 Release。</p></html>',encoding='utf-8')
    return catalog


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True)
    parser.add_argument('--source-ref',default='main')
    args=parser.parse_args()
    result=build_site(ROOT,args.output,args.source_ref)
    print(json.dumps({'packages':len(result['packages']),'source_commit':result['source_commit']}))
