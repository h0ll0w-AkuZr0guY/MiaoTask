"""Distribution constants shared by the installer, build tools and verification."""
import keyword
import re

FRAMEWORK_API = 1
PACKAGE_SCHEMA = 1
OFFICIAL_REPOSITORY = 'h0ll0w-AkuZr0guY/MiaoTask'
SOURCE_REF = 'main'
CATALOG_URL = f'https://api.github.com/repos/{OFFICIAL_REPOSITORY}/git/ref/heads/{SOURCE_REF}'
MAX_TREE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_FILE_BYTES = 5 * 1024 * 1024
# The initially published manifests predate explicit compatibility declarations.
# This pair alone has been reviewed against API 1 / Python 3.13. New or changed
# activities declare compat.json instead of silently inheriting that assumption.
LEGACY_COMPATIBLE_MANIFESTS = frozenset({(
    'aeecb87f7193a23b1da066a101496e683a63bc716cefaa47adcd8e7394f65a85',
    '762ecc4a1eb00747a20204277a454cfd087d3be31c7ea06e4eeb0721a7f3ab7f',
)})
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
