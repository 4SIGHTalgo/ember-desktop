"""DraftKings login module: local permission grants, no credentials or forms."""
import unittest
from unittest.mock import Mock, call

from selenium.common.exceptions import InvalidArgumentException, WebDriverException

from draftkings_login import LOCATION_PERMISSION_NAMES, LOCATION_PERMISSION_ORIGINS, allow_location_permissions

CONTEXT = "0CD6607F689523E6C236C1A83FC28443"
TARGET_INFO = {"targetInfo": {"browserContextId": CONTEXT}}


def grants():
    return [{"origin": origin, "permission": {"name": name}, "setting": "granted"}
            for origin in LOCATION_PERMISSION_ORIGINS for name in LOCATION_PERMISSION_NAMES]


class AllowLocationPermissionsTests(unittest.TestCase):
    def set_permission_calls(self, driver):
        return [item for item in driver.execute_cdp_cmd.call_args_list
                if item.args[0] == "Browser.setPermission"]

    def test_resolvable_context_is_targeted_on_every_grant(self):
        driver = Mock()
        driver.execute_cdp_cmd.side_effect = [TARGET_INFO] + [None]*80
        allow_location_permissions(driver)
        self.assertEqual(driver.execute_cdp_cmd.call_args_list[0], call("Target.getTargetInfo", {}))
        permissions = self.set_permission_calls(driver)
        self.assertEqual(len(permissions), len(grants()))
        for item, grant in zip(permissions, grants()):
            self.assertEqual(item.args[1], {**grant, "browserContextId": CONTEXT})

    def test_unresolvable_context_falls_back_to_the_default_context_once(self):
        driver = Mock()
        def set_permission(command, payload):
            if command == "Target.getTargetInfo":
                return TARGET_INFO
            # shortcut: only Chrome's context-resolution rejection falls back.
            if payload.get("browserContextId") == CONTEXT:
                raise InvalidArgumentException(
                    "invalid argument: Failed to find browser context for id " + CONTEXT)
        driver.execute_cdp_cmd.side_effect = set_permission
        allow_location_permissions(driver)
        permissions = self.set_permission_calls(driver)
        self.assertEqual(len(permissions), len(grants())+1)
        self.assertEqual(permissions[0].args[1]["browserContextId"], CONTEXT)
        retried = permissions[1].args[1]
        self.assertNotIn("browserContextId", retried)
        self.assertEqual(retried["origin"], permissions[0].args[1]["origin"])
        self.assertEqual(retried["permission"], permissions[0].args[1]["permission"])
        for item in permissions[2:]:
            self.assertNotIn("browserContextId", item.args[1])

    def test_other_rejections_stay_fail_closed(self):
        driver = Mock()
        driver.execute_cdp_cmd.side_effect = [TARGET_INFO, InvalidArgumentException("permission unknown")]
        with self.assertRaises(InvalidArgumentException):
            allow_location_permissions(driver)
        self.assertEqual(len(self.set_permission_calls(driver)), 1)

    def test_absent_context_grants_without_an_id(self):
        driver = Mock()
        driver.execute_cdp_cmd.side_effect = [{"targetInfo": {}}] + [None]*80
        allow_location_permissions(driver)
        for item in self.set_permission_calls(driver):
            self.assertNotIn("browserContextId", item.args[1])
        self.assertEqual(len(self.set_permission_calls(driver)), len(grants()))


if __name__ == "__main__":
    unittest.main()