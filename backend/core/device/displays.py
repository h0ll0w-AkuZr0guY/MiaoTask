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
        if re.search(r'\bmDisplayId\s*=\s*%d\b' % logical_id, stripped):
            # A few ROMs put the display id on the same line as the section
            # header, or omit the "Display N:" header entirely.
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
        direct = re.search(r'mPrimaryDisplayDevice=[^\n]*\(local:(\d+)\)', block)
        if direct:
            return direct[1]
        own = re.search(r'mBaseDisplayInfo=([^\n]+)', block)
        own_id = re.search(r'uniqueId[= ]"?local:(\d+)', own[1]) if own else None
        if own_id:
            return own_id[1]
        named = re.search(r'mPrimaryDisplayDevice\s*=\s*([^\n(]+)', block)
        if named:
            name = re.escape(named[1].strip().strip('"'))
            matches = set(re.findall(
                r'DisplayDeviceInfo\{\s*["\']?' + name +
                r'["\']?\s*:\s*[^\n]*?uniqueId\s*=\s*["\']?local:(\d+)',
                display_dump))
            if len(matches) == 1:
                return matches.pop()
    # Single-display ROMs sometimes omit the primary-device name and the
    # logical block entirely.  A unique physical id is still unambiguous.
    physical_ids = set(re.findall(r'\blocal:(\d+)\b', display_dump))
    if (not block and not re.search(r'\bmDisplayId\s*=', display_dump)
            and len(physical_ids) == 1):
        return physical_ids.pop()
    raise RuntimeError('无法映射当前应用屏幕，已停止截图和点击；请检查模拟器多屏状态')
