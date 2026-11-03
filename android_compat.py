"""Android focus detection compatible with newer dumpsys output."""
import re
import uiautomator2 as u2

def install():
    if getattr(u2.Device, '_pilot_focus_patch', False):
        return
    original = u2.Device.app_current
    def app_current(device):
        output = device.adb_device.shell(['dumpsys', 'window'])
        match = re.search(r'mCurrentFocus=Window\{[^\n]*?\s([^\s/]+)/([^\s}]+)\}', output)
        if match:
            return {'package':match[1], 'activity':match[2], 'pid':0}
        return original(device)
    u2.Device.app_current = app_current
    u2.Device._pilot_focus_patch = True

install()
