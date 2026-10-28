from backend.core.registry import ActivityMeta

META = ActivityMeta('taobao', 'cash-signin', '红包签到',
    'backend.app.taobao.activities.cash_signin.activity:CashSigninActivity', device_types=('uiautomator2',))
