from backend.core.registry import ActivityMeta

META = ActivityMeta('taobao', 'achievements', '淘宝成就中心',
    'backend.app.taobao.activities.achievements.activity:AchievementsActivity', device_types=('uiautomator2',))
