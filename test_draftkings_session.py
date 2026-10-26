import unittest
from unittest.mock import Mock

from driver_lifecycle import DriverConnectionLost
from draftkings_session import SessionMaintenance


class FatalMaintenanceTests(unittest.TestCase):
    def test_optional_network_observation_never_swallows_a_fatal_driver_failure(self):
        driver=Mock();driver.get_log.side_effect=DriverConnectionLost('synthetic reply lost')
        with self.assertRaises(DriverConnectionLost):
            SessionMaintenance(45).observe_network(driver,0)
        driver.execute_script.assert_not_called()


if __name__=='__main__':
    unittest.main()
