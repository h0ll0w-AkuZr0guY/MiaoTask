"""Double 11 assistant: local web panel by default, or a scriptable CLI."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

# Use the local tools copy for source development and bundled tools when frozen.
tools_dir = (Path(sys._MEIPASS) / 'shared/tools' if getattr(sys, 'frozen', False)
             else Path(__file__).resolve().parents[1] / 'build/vendor/platform-tools')
if tools_dir.is_dir():
    os.environ['PATH'] = str(tools_dir) + os.pathsep + os.environ.get('PATH', '')
    adb = tools_dir / 'adb.exe'
    if adb.exists():
        os.environ['ADBUTILS_ADB_PATH'] = str(adb)


def main():
    parser = argparse.ArgumentParser(description='MiaoTask：本地网页控制台 / 命令行')
    parser.add_argument('--app', help='应用 ID，默认使用注册表默认项')
    parser.add_argument('--activity', help='活动 ID，默认使用所选应用默认活动')
    parser.add_argument('--list-activities', action='store_true', help='列出已注册活动，不连接设备')
    parser.add_argument('--cli', action='store_true', help='使用命令行执行任务')
    parser.add_argument('--port', type=int, default=8765, help='本机控制台端口，默认 8765')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--self-test', action='store_true', help='检查打包资源、ADB 和离线 OCR，不操作手机')
    parser.add_argument('--output', help='自检 JSON 输出文件')
    parser.add_argument('--activity-package', help='仅用于 --self-test：安装并验证本地可信活动 ZIP')
    parser.add_argument('--activity-package-sha256', help='本地活动 ZIP 的预期 SHA256，须与包路径同时指定')
    parser.add_argument('--port-file', help='将服务 URL 写入指定文件，供打包验证使用')
    parser.add_argument('--device', choices=['android', 'harmony'], default='android')
    parser.add_argument('--serial', help='USB 序列号或已连接的无线调试地址')
    parser.add_argument('--instance', help='CLI 指定包名@用户空间，例 com.taobao.taobao@999')
    parser.add_argument('--auto-open', action='store_true', help='尝试从淘宝首页自动进入活动')
    parser.add_argument('--scan', action='store_true', help='CLI 仅识别任务，不执行')
    parser.add_argument('--config', help='覆盖默认配置的 JSON 文件')
    parser.add_argument('--max-tasks', type=int, help='本轮最大尝试次数')
    parser.add_argument('--no-ocr', action='store_true', help='关闭本地文字识别')
    parser.add_argument('--no-cross-app', action='store_true', help='跳过跨应用任务')
    parser.add_argument('--log', dest='loglevel', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])
    args = parser.parse_args()
    if bool(args.activity_package) != bool(args.activity_package_sha256):
        parser.error('--activity-package 与 --activity-package-sha256 必须同时提供')
    if args.activity_package and not args.self_test:
        parser.error('本地活动包验证仅支持 --self-test')
    from backend.core.runtime import data_dir
    handlers = [RotatingFileHandler(data_dir() / 'application.log', maxBytes=2_000_000,
                                   backupCount=2, encoding='utf-8')]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=getattr(logging, args.loglevel), handlers=handlers,
                        format='%(asctime)s %(levelname)-7s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    if args.self_test:
        from backend.core.service.diagnostics import self_test
        result = self_test(args.output or str(data_dir() / 'self-test.json'),
                           activity_package=args.activity_package,
                           activity_sha256=args.activity_package_sha256)
        if sys.stdout is not None:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result['ok'] else 1)
    from backend.bootstrap import create_runner as CampaignRunner, load_config, validate_config
    from backend.core.errors import NeedsAttention, Stopped
    from backend.core.registry import default_registry
    registry = default_registry()
    if args.list_activities:
        print(json.dumps(registry.catalog(), ensure_ascii=False, indent=2))
        return
    config = load_config(args.config, app=args.app, activity=args.activity)
    if args.max_tasks is not None:
        config['max_tasks'] = args.max_tasks
    if args.no_ocr:
        config['ocr'] = False
    if args.no_cross_app:
        config['allow_cross_app'] = False
    if config:
        validate_config(config)
    if not args.cli:
        from backend.core.service.panel import serve
        try:
            serve(args.port, not args.no_browser, config, port_file=args.port_file, app=args.app, activity=args.activity)
        except OSError as exc:
            parser.exit(1, f'控制台启动失败：{exc}，可通过 --port 指定其他端口\n')
        return
    if not registry.has_activities:
        parser.exit(2, '尚未安装活动，请启动网页控制台，在活动中心下载所需活动后再使用 CLI\n')
    from backend.core.device.adapter import DeviceAdapter
    from backend.core.device.instances import InstanceMismatch
    device = DeviceAdapter('hmdriver2' if args.device == 'harmony' else 'uiautomator2', args.serial, application=registry.get(args.app,args.activity).app.profile)
    if args.instance:
        device.select_app_instance(args.instance)
    runner = CampaignRunner(device, config, app=args.app, activity=args.activity)
    try:
        with device.instance_session():
            result = runner.scan(args.auto_open) if args.scan else runner.run(args.auto_open)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (NeedsAttention, Stopped, InstanceMismatch) as exc:
        parser.exit(2, f'{exc}\n')
    except KeyboardInterrupt:
        runner.control.stopped.set()
        print('\n已停止')


def launch():
    try:
        main()
    except Exception:
        logging.exception('启动失败')
        if getattr(sys, 'frozen', False):
            import ctypes
            from backend.core.runtime import data_dir
            ctypes.windll.user32.MessageBoxW(0, f'启动失败，请查看日志：\n{data_dir() / "application.log"}', '喵任务', 16)
        raise


if __name__ == "__main__":
    launch()
