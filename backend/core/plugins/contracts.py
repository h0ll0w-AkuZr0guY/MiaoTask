"""Distribution constants shared by the installer, build tools and verification."""
import keyword
import re

FRAMEWORK_API = 1
PACKAGE_SCHEMA = 1
OFFICIAL_REPOSITORY = 'h0ll0w-AkuZr0guY/MiaoTask'
CATALOG_URL = f'https://github.com/{OFFICIAL_REPOSITORY}/releases/download/activity-packages/activity-catalog.json'
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 50 * 1024 * 1024
MAX_FILES = 500
MAX_CATALOG_BYTES = 2 * 1024 * 1024
ALLOWED_SUFFIXES = {'.py', '.json', '.yaml', '.yml', '.txt', '.md', '.png', '.jpg', '.jpeg', '.webp'}


def module_id(value):
    if (not isinstance(value, str) or not re.fullmatch(r'[a-z][a-z0-9_]*', value)
            or keyword.iskeyword(value) or value == 'common'):
        raise ValueError('活动包 Python 模块名称无效')
    return value


def package_version(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?', value):
        raise ValueError('活动包版本必须使用语义化版本号')
    return value
