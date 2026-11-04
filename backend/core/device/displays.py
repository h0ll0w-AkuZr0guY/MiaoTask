"""Logical Android display targets and SurfaceFlinger capture identifiers."""
import re


def focused_display(window_dump):
    match = re.search(r'mTopFocusedDisplayId\s*=\s*(\d+)', window_dump)
    return int(match[1]) if match else 0


def focused_window_dump(displays_dump, logical_id):
    for block in re.split(r'(?=^\s*Display: mDisplayId=)', displays_dump, flags=re.M):
        match=re.search(r'Display: mDisplayId=(\d+)',block)
        if match and int(match[1])==logical_id:
            return block
    return ''


def _logical_display_block(display_dump, logical_id):
    """Return the dumpsys section describing one logical display.

    ROMs differ in the section header: newer builds print
    ``Display: mDisplayId=N`` while some MTK builds print ``Display N:`` with
    the id on a following record. Line scanning keeps both layouts covered.
    """
    lines = display_dump.splitlines()
    start = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if re.fullmatch(r'Display: mDisplayId=%d' % logical_id, stripped):
            start = index
            break
        if re.fullmatch(r'Display \d+:', stripped) and index + 1 < len(lines) \
                and lines[index + 1].strip() == 'mDisplayId=%d' % logical_id:
            start = index
            break
    if start is None:
        return ''
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if re.fullmatch(r'(?:Display: mDisplayId=\d+|Display \d+:)', lines[index].strip()):
            end = index
            break
    return '\n'.join(lines[start:end])


def capture_display(display_dump, logical_id):
    # Logical ID, HWC port and adbutils ordinal are three different namespaces.
    # Resolve through Android's explicit primary-device uniqueId mapping.
    logical_id = int(logical_id)
    match = re.search(r'mDisplayId=' + str(logical_id) + r'\s+mPrimaryDisplayDevice=[^\n]*\(local:(\d+)\)',
                      display_dump)
    if match:
        return match[1]
    block = _logical_display_block(display_dump, logical_id)
    if block:
        # Some ROMs print the primary device name only, without its (local:N)
        # port; look the name up in the physical-device inventory instead.
        named = re.search(r'mPrimaryDisplayDevice=([^\n(]+)', block)
        if named:
            device = re.search(r'DisplayDeviceInfo\{"' + re.escape(named[1].strip())
                               + r'":\s*uniqueId="local:(\d+)"', display_dump)
            if device:
                return device[1]
        # Last resort on single-display devices: the logical display's own
        # mBaseDisplayInfo carries the physical uniqueId of its source device.
        own = re.search(r'uniqueId[= ]"?local:(\d+)', block)
        if own:
            return own[1]
    raise RuntimeError('无法映射当前应用屏幕，已停止截图和点击；请检查模拟器多屏状态')
