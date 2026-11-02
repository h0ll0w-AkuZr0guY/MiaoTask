"""Visual row pairing; platform vocabulary is injected as TaskRules."""
import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Pattern
from backend.core.models import Task, TextNode, normalize, bounds_of

@dataclass(frozen=True)
class TaskRules:
    action_re: Pattern
    progress_re: Pattern
    description_re: Pattern
    ignored_titles: frozenset
    milestone_header: str
    milestone_action_re: Pattern
    milestone_title: str
    classify: Callable

def parse_tasks(nodes, width=1440, height=3200, *, rules):
    """Pair right-hand action buttons with text in the same visual row.

    Geometry handles Themis pages whose accessibility siblings are unrelated,
    and works identically when the page exposes only a bitmap.
    """
    header = next((n for n in nodes if normalize(n.text) == rules.milestone_header), None)
    if header and header.bounds[0] > width * .15:
        # A centered sheet on a tablet leaves product text visible beside it.
        # Infer its horizontal content band from the header and action controls.
        buttons = [n for n in nodes if rules.action_re.fullmatch(normalize(n.text))
                   and n.center[0] > header.center[0]]
        if buttons:
            left = max(0, header.bounds[0] - width * .035)
            right = min(width, max(n.bounds[2] for n in buttons) + width * .035)
            nodes = [n for n in nodes if left <= n.center[0] <= right]
    actions = []
    for node in nodes:
        label = normalize(node.text)
        x, y = node.center
        if rules.action_re.fullmatch(label) and x > width * .55 and 0 < y < height * .985:
            if not any(abs(y - old.center[1]) < height * .012 for old in actions):
                actions.append(node)
    actions.sort(key=lambda n: n.center[1])
    tasks = []
    for idx, button in enumerate(actions):
        y = button.center[1]
        prev = actions[idx - 1].center[1] if idx else y - height * .16
        nxt = actions[idx + 1].center[1] if idx + 1 < len(actions) else y + height * .16
        top = max((prev + y) / 2, y - height * .055)
        bottom = min((nxt + y) / 2, y + height * .042)
        row = []
        for n in nodes:
            x0, y0 = n.center
            if top <= y0 <= bottom and n.bounds[0] < button.bounds[0] - width * .025:
                text = normalize(n.text)
                if text and not rules.action_re.fullmatch(text) and n.bounds[3] - n.bounds[1] < height * .1:
                    row.append(n)
        row.sort(key=lambda n: (round(n.center[1] / (height * .008)), n.bounds[0]))
        texts = list(dict.fromkeys(normalize(n.text) for n in row
                                   if not re.search(r'O1CN|!!\d+|\.(?:png|jpg|webp)|^\W+$', n.text)))
        # Do not guess what an unlabelled button does.
        if not texts:
            continue
        title = next((t for t in texts if rules.progress_re.search(t)), texts[0])
        if title in rules.ignored_titles:
            continue
        detail = " ".join(t for t in texts if t != title)
        progress = rules.progress_re.search(title)
        # During reward animations OCR can read a toast over a task's title.
        # Wait for a real progress label instead of treating that text as a task.
        if not progress:
            continue
        current, total = map(int, progress.groups()) if progress else (0, 1)
        # Some WebViews expose the modal's complete row in one node while
        # continuing to expose the obscured homepage underneath. Once this
        # node already includes its description, nearby background text must
        # not contribute payment/game keywords or app names to this task.
        suffix = title[progress.end():].strip()
        if suffix and row and any(n.source == 'ui' and normalize(n.text) == title for n in row):
            texts = [title]
            detail = suffix
            title = title[:progress.start()].strip()
        else:
            title = rules.progress_re.sub("", title).strip()
        # Themis sometimes exposes a whole row as one accessibility string,
        # whereas OCR separates the title and description into two lines.
        description = rules.description_re.search(title)
        if description:
            suffix = title[description.start():]
            title = title[:description.start()].strip()
            if suffix not in detail:
                detail = (detail + ' ' + suffix).strip()
        if not title:
            continue
        action = normalize(button.text)
        seconds, kind, reason, target_app, packages = rules.classify(title, detail, texts, action, current, total)
        key = hashlib.sha256(normalize(title).encode("utf-8")).hexdigest()[:16]
        tasks.append(Task(key, title, detail, action, button.bounds, current, total,
                          seconds, kind, reason, target_app, packages))
    # Cumulative rewards live above the list and are not right-hand row buttons.
    header = next((n for n in nodes if normalize(n.text) == rules.milestone_header), None)
    if header:
        reward_bottom = min((t.bounds[1] for t in tasks), default=height)
        reward_keys = set()
        for n in nodes:
            label = normalize(n.text)
            if (rules.milestone_action_re.search(label)
                    and n.bounds[1] >= header.bounds[1] - height * .01
                    and n.bounds[3] < reward_bottom
                    and n.bounds[3] - n.bounds[1] < height * .1
                    and n.bounds[2] - n.bounds[0] < width * .5):
                key = f'milestone-{round(n.center[0] / width * 10)}'
                if key not in reward_keys:
                    reward_keys.add(key)
                    tasks.append(Task(key, rules.milestone_title, label, '立即领取', n.bounds, kind='claim'))
    return tasks
