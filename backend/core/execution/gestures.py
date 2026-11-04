"""Bounded, configurable random scroll gestures."""
import random


SWIPE_DEFAULTS = dict(swipe_distance_min=.18, swipe_distance_max=.36,
                      swipe_duration_min=.25, swipe_duration_max=.65,
                      swipe_interval_min=0, swipe_interval_max=0,
                      swipe_burst_min=1, swipe_burst_max=3)


def validate_swipes(config):
    limits = dict(distance=(.08, .5), duration=(.15, 1.5), interval=(0, 3), burst=(1, 5))
    for name, (low, high) in limits.items():
        keys = [f'swipe_{name}_{end}' for end in ('min', 'max')]
        for key in keys:
            value = config.setdefault(key, SWIPE_DEFAULTS[key])
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not low <= value <= high or name == 'burst' and not isinstance(value, int)):
                raise ValueError(f'{key} 必须在 {low}～{high} 之间' + ('且为整数' if name == 'burst' else ''))
        if config[keys[0]] > config[keys[1]]:
            raise ValueError(f'swipe_{name} 最小值不能大于最大值')


class BrowseGestures:
    def __init__(self, config, rng=None):
        self.config = config
        self.rng = rng or random.SystemRandom()
        self.reverse = True
        self.left = 0

    def sample(self, width, height):
        c, rng = self.config, self.rng
        if self.left == 0:
            self.reverse = not self.reverse
            self.left = rng.randint(c['swipe_burst_min'], c['swipe_burst_max'])
        self.left -= 1
        distance = rng.uniform(c['swipe_distance_min'], c['swipe_distance_max'])
        # Keep all endpoints away from top chrome, bottom navigation and edges.
        start = rng.uniform(.25, min(.43, .86-distance)) if self.reverse else rng.uniform(max(.60, .18+distance), .82)
        end = start + distance if self.reverse else start - distance
        x1 = rng.uniform(.42, .58)
        x2 = min(.62, max(.38, x1 + rng.uniform(-.035, .035)))
        return (round(width*x1), round(height*start), round(width*x2), round(height*end),
                rng.uniform(c['swipe_duration_min'], c['swipe_duration_max']))

    def interval(self):
        return self.rng.uniform(self.config['swipe_interval_min'], self.config['swipe_interval_max'])
