"""Publish the current frontend with an isolated, browser-only demonstration adapter."""
import argparse
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def build_preview(root, output):
    from backend.core.registry import Registry
    root, output = Path(root), Path(output)
    if output.resolve() == root.resolve() or output.resolve().is_relative_to((root / 'frontend').resolve()):
        raise ValueError('Preview output must be separate from frontend source')
    output.mkdir(parents=True, exist_ok=True)
    for name in ('assets', 'css', 'js'):
        shutil.copytree(root / 'frontend' / name, output / name, dirs_exist_ok=True)
    demo = output / 'demo'
    shutil.copytree(root / 'scripts/site-preview', demo, dirs_exist_ok=True)
    shutil.copyfile(root / 'docs/assets/demo-phone.svg', demo / 'phone.svg')
    registry = Registry(root / 'backend/app').discover()
    data = {'catalog': registry.catalog(), 'version': json.loads((root / 'version.json').read_text(encoding='utf-8'))['version']}
    # JSON stays a standalone resource, never injected as executable HTML.
    (demo / 'catalog.json').write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    html = (root / 'frontend/index.html').read_text(encoding='utf-8')
    html = html.replace('src="/', 'src="./').replace('href="/', 'href="./').replace('srcset="/', 'srcset="./')
    html = html.replace('<head>', '<head>\n  <script src="./demo/adapter.js"></script><link rel="stylesheet" href="./demo/preview.css">', 1)
    html = html.replace('<main>', '<main><section class="demo-notice" role="note"><strong>在线交互预览</strong><span>界面自动同步 main；设备、画面与统计均为演示数据，不能连接手机或执行任务。选择与设置仅在当前页面有效。</span><a href="https://github.com/h0ll0w-AkuZr0guY/MiaoTask/releases/latest">下载桌面版 ↗</a><a href="./activities/">活动资源</a></section>', 1)
    html = html.replace(' 本地运行</div>', ' 在线演示</div>')
    html = html.replace('仅本机访问 · 截图识别在本地完成', '在线交互预览 · 数据均为演示')
    (output / 'index.html').write_text(html, encoding='utf-8')
    api_path = output / 'js/api.js'
    api = api_path.read_text(encoding='utf-8')
    needle = 'url(path, device=selectedDevice){'
    if api.count(needle) != 1:
        raise ValueError('Frontend API URL contract changed; update preview adapter')
    api = api.replace(needle, needle + "if(path==='/api/screenshot')return './demo/phone.svg?demo=1';", 1)
    api_path.write_text(api, encoding='utf-8')
    (output / '.nojekyll').touch()
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = build_preview(ROOT, args.output)
    print(json.dumps({'preview': args.output, 'applications': len(result['catalog'])}))
