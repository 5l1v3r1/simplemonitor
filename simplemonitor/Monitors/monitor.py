# coding=utf-8
"""A collection of monitors for the SimpleMonitor application.

The Monitor class contains the monitor-independent logic for handling results etc.

Subclasses should provide an __init__(), and override at least run_test() to actually
perform the test. A successful test should call self.record_success() and a failed one
should call self.record_fail(). You should also override the describe() and get_params()
functions.

"""

import copy
import datetime
import logging
import platform
import subprocess  # nosec
import time
from typing import Any, List, NoReturn, Optional, Tuple, Union, cast

from ..util import (
    MonitorConfigurationError,
    MonitorState,
    UpDownTime,
    format_datetime,
    get_config_option,
    short_hostname,
    subclass_dict_handler,
)


class Monitor:
    """Simple monitor. This class is abstract."""

    _type = "unknown"
    last_result = ""
    error_count = 0
    _failed_at = None
    success_count = 0
    tests_run = 0
    last_error_count = 0
    last_run_duration = 0
    skip_dep = None  # type: Optional[str]

    failures = 0
    last_failure = None  # type: Optional[datetime.datetime]
    uptime_start = None  # type: Optional[datetime.datetime]

    # this is the time we last received data into this monitor (if we're remote)
    last_update = None  # type: Optional[datetime.datetime]

    _first_load = None  # type: Optional[datetime.datetime]
    unavailable_seconds = 0  # type: int

    def __init__(
        self, name: str = "unnamed", config_options: Optional[dict] = None
    ) -> None:
        """What's that coming over the hill? Is a monitor?"""
        if config_options is None:
            config_options = {}
        self._config_options = config_options
        self.name = name
        self._deps = []  # type: List[str]
        self.monitor_logger = logging.getLogger("simplemonitor.monitor-" + self.name)
        self._dependencies = self.get_config_option(
            "depend", required_type="[str]", default=list()
        )
        self._urgent = self.get_config_option(
            "urgent", required_type="bool", default=True
        )
        self._notify = self.get_config_option(
            "notify", required_type="bool", default=True
        )
        self.group = self.get_config_option("group", default="default")
        self._tolerance = self.get_config_option(
            "tolerance", required_type="int", default=0, minimum=0
        )
        self.remote_alerting = cast(
            bool,
            self.get_config_option("remote_alert", required_type="bool", default=False),
        )
        self._recover_command = self.get_config_option("recover_command")
        self._recovered_command = self.get_config_option("recovered_command")
        self.recover_info = ""
        self.recovered_info = ""
        self.minimum_gap = self.get_config_option(
            "gap", required_type="int", minimum=0, default=0
        )
        self.failure_doc = cast(
            Optional[str], self.get_config_option("failure_doc", default=None)
        )

        self.running_on = short_hostname()
        self._state = MonitorState.UNKNOWN
        self._force_run = True  # set to ensure we re-run ASAP after a HUP
        if self._first_load is None:
            self._first_load = datetime.datetime.utcnow()

    def get_config_option(self, key: str, **kwargs: Any) -> Any:
        kwargs["exception"] = MonitorConfigurationError
        return get_config_option(self._config_options, key, **kwargs)

    @property
    def dependencies(self) -> List[str]:
        """The Monitors we depend on.
        If a monitor we depend on fails, we will skip"""
        return self._dependencies

    @dependencies.setter
    def dependencies(self, dependency_list: List[str]) -> None:
        if not isinstance(dependency_list, list):
            raise TypeError("dependency_list must be a list")
        self._dependencies = dependency_list
        self.reset_dependencies()

    @property
    def remaining_dependencies(self) -> List[str]:
        """The Monitors we still depend on for this loop"""
        return self._deps

    def is_remote(self) -> bool:
        """Check if we're running on this machine, or if we're a remote instance."""
        if self.running_on == short_hostname():
            return False
        return True

    def run_test(self) -> Union[NoReturn, bool]:
        """Override this method to perform the test."""
        raise NotImplementedError

    def virtual_fail_count(self) -> int:
        """Return the number of failures we've had past our tolerance."""
        vfs = self.error_count - self._tolerance
        return max(vfs, 0)

    def test_success(self) -> bool:
        """Returns false if the monitor has failed.

        This means that enough tests have failed in a row to exceed the tolerance."""
        return not bool(self.virtual_fail_count())

    def first_failure(self) -> bool:
        """Check if this is our first failure (past tolerance)."""
        if self.error_count == (self._tolerance + 1):
            return True
        return False

    def state(self) -> MonitorState:
        return self._state

    def get_result(self) -> str:
        """Return the result info from the last test."""
        return self.last_result

    def reset_dependencies(self) -> None:
        """Reset the monitor's dependency list back to default."""
        self._deps = copy.copy(self._dependencies)

    def dependency_succeeded(self, dependency: str) -> None:
        """Remove a dependency from the current version of the list."""
        try:
            self._deps.remove(dependency)
        except ValueError:
            pass

    def log_result(self, name: str, logger: Any) -> None:
        """Save our latest result to the logger.

        TODO: remove when known safe"""
        self.monitor_logger.critical("Unexpected call to log_result()")

    def get_params(self) -> Tuple:
        """Override this method to return a list of parameters (for logging)"""
        raise NotImplementedError

    def set_mon_refs(self, mmm: Any) -> None:
        """Called with a reference to the list of all monitors.
        Only used by CompoundMonitor for now."""
        pass

    @property
    def minimum_gap(self) -> int:
        """Minimum gap between runs of the monitor."""
        return self._minimum_gap

    @minimum_gap.setter
    def minimum_gap(self, gap: int) -> None:
        if isinstance(gap, int):
            if gap < 0:
                raise ValueError("gap must be at least 0")
            self._minimum_gap = int(gap)
        else:
            raise TypeError("gap must be an integer")

    def describe(self) -> str:
        """Explain what this monitor does.
        We don't throw NotImplementedError here as it won't show up until something breaks,
        and we don't want to randomly die then."""
        return "(Monitor did not write an auto-biography.)"

    @staticmethod
    def is_windows(allow_cygwin: bool = True) -> bool:
        """Checks if our platform is Windowsy.
        If allow_cygwin is False, cygwin will be reported as UNIX."""

        platforms = ["Microsoft", "Windows"]
        if allow_cygwin:
            platforms.append("CYGWIN_NT-6.0")

        if platform.system() in platforms:
            return True
        return False

    def _add_unavailable_seconds(self) -> None:
        if self.last_update and self.success_count == 0:
            unavailable_delta = datetime.datetime.utcnow() - self.last_update
            self.unavailable_seconds += unavailable_delta.seconds

    def record_fail(self, message: str = "") -> bool:
        """Update internal state to show that we had a failure."""
        self.error_count += 1
        self._add_unavailable_seconds()
        self.last_update = datetime.datetime.utcnow()
        self.last_result = str(message)
        if self.virtual_fail_count() == 1:
            self._failed_at = datetime.datetime.utcnow()
            self.last_failure = datetime.datetime.utcnow()
            self.failures += 1
            self._state = MonitorState.FAILED
        self.success_count = 0
        self.tests_run += 1
        self.uptime_start = None
        return False

    def record_success(self, message: str = "") -> bool:
        """Update internal state to show we had a success."""
        if self.error_count > 0:
            self.last_error_count = self.error_count
        if self.uptime_start is None:
            self.uptime_start = datetime.datetime.utcnow()
        self._add_unavailable_seconds()
        self._state = MonitorState.OK
        self.error_count = 0
        self.last_update = datetime.datetime.utcnow()
        self.success_count += 1
        self.tests_run += 1
        self.last_result = message
        return True

    def record_skip(self, which_dep: Optional[str]) -> bool:
        """Record that we were skipped.

        We pretend to have succeeded as we don't want notifications sent."""
        if which_dep is not None:
            # we were skipped because of a dependency
            self.record_success()
            self.skip_dep = which_dep
        self._state = MonitorState.SKIPPED
        return True

    def uptime(self) -> Optional[datetime.timedelta]:
        if self.uptime_start:
            return datetime.datetime.utcnow() - self.uptime_start
        return None

    def skipped(self) -> bool:
        if self._state == MonitorState.SKIPPED:
            return True
        return False

    def get_success_count(self) -> int:
        """Get the number of successful tests."""
        if self.tests_run == 0:
            return 0
        return self.success_count

    def all_better_now(self) -> bool:
        """Check if we've just recovered."""
        if (
            self.last_virtual_fail_count()
            and self.success_count == 1
            and not self._state == MonitorState.SKIPPED
        ):
            return True
        return False

    @property
    def availability(self) -> float:
        if self.tests_run <= 1:
            return 0.0
        if self._first_load is not None:
            total_seconds = (
                datetime.datetime.utcnow() - self._first_load
            ).total_seconds()
            availability = 1 - (self.unavailable_seconds / total_seconds)
        else:
            availability = 0.0
        return availability

    def first_failure_time(self) -> Optional[datetime.datetime]:
        """Get a datetime object showing when we first failed."""
        return self._failed_at

    @property
    def notify(self) -> bool:
        return self._notify

    @notify.setter
    def notify(self, value: bool) -> None:
        if isinstance(value, bool):
            self._notify = value
        else:
            raise TypeError("notify must be a bool")

    @property
    def urgent(self) -> bool:
        return self._urgent

    @urgent.setter
    def urgent(self, value: Union[bool, int]) -> None:
        if isinstance(value, bool):
            self._urgent = value
        elif isinstance(value, int):
            if value:
                self._urgent = True
            else:
                self._urgent = False
        else:
            raise TypeError("urgent should be a bool, or an int at a push")

    @property
    def was_skipped(self) -> bool:
        return self._state == MonitorState.SKIPPED

    def should_run(self) -> bool:
        """Check if we should run our tests.

        We always run if the minimum gap is 0, or if we're currently failing.
        Otherwise, we run if the last time we ran was more than minimum_gap seconds ago.
        """
        now = int(time.time())
        if self._force_run:
            self._force_run = False
            self._last_run = now
            return True
        if self.minimum_gap == 0:
            self._last_run = now
            return True
        if self.error_count > 0:
            self._last_run = now
            return True
        if self._last_run == 0:
            self._last_run = now
            return True
        gap = now - self._last_run
        if gap >= self.minimum_gap:
            self._last_run = now
            return True
        return False

    def last_virtual_fail_count(self) -> int:
        value = self.last_error_count - self._tolerance
        return max(0, value)

    def attempt_recover(self) -> None:
        if self._recover_command is None:
            self.recover_info = ""
            return
        if not self.first_failure():
            return

        try:
            self.monitor_logger.info("Attempting recovery command")
            p = subprocess.Popen(self._recover_command.split(" "))  # nosec
            p.wait()
            self.recover_info = "Command executed and returned %d" % p.returncode
        except Exception as e:
            self.recover_info = "Unable to run command: %s" % e

    def run_recovered(self) -> None:
        if self._recovered_command is None:
            self.recovered_info = ""
            return
        if self.all_better_now():
            self.monitor_logger.info("Attempting recovered command")
            try:
                p = subprocess.Popen(self._recovered_command.split(" "))  # nosec
                p.wait()
                self.recovered_info = "Command executed and returned %d" % p.returncode
            except Exception as e:
                self.recovered_info = "Unable to run command: %s" % e

    def post_config_setup(self) -> None:
        """ any post config setup needed """
        pass

    def __getstate__(self) -> dict:
        """Loggers (the Python kind, not the SimpleMonitor kind) can't be serialized.
        In order to work around that, we omit them when getting serialized (for
        being sent over the network).
        """
        serialize_dict = dict(self.__dict__)
        del serialize_dict["monitor_logger"]
        return serialize_dict

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._set_monitor_logger()

    def _set_monitor_logger(self) -> None:
        self.monitor_logger = logging.getLogger("simplemonitor.monitor-" + self.name)

    def to_python_dict(self) -> dict:
        return self.__getstate__()

    """
    TODO
    from typing import Type, TypeVar

    T = TypeVar('T', bound='TrivialClass')

    class TrivialClass:
        # ...

    @classmethod
    def from_int(cls: Type[T], int_arg: int) -> T:
        # ...
        return cls(...)
    """

    @classmethod
    def from_python_dict(
        cls: Any, d: dict
    ) -> "Monitor":  # can't return Monitor type as flake8 gets cross
        monitor = Monitor()
        monitor.__class__ = cls
        monitor.__setstate__(d)
        return monitor

    def get_downtime(self) -> UpDownTime:
        """Get monitor downtime"""
        first_failure_time = self.first_failure_time()
        if first_failure_time is None:
            return UpDownTime()
        else:
            downtime = datetime.datetime.utcnow() - first_failure_time
            return UpDownTime.from_timedelta(downtime)

    def get_uptime(self) -> UpDownTime:
        """Get monitor uptime"""
        uptime = self.uptime()
        if uptime is None:
            return UpDownTime()
        return UpDownTime.from_timedelta(uptime)

    def state_dict(self) -> dict:
        """Get a dict containing state information about the Monitor to use for logging or alerting"""
        ret = {
            "failed_at": format_datetime(self.first_failure_time()),
            "name": self.name,
            "host": self.running_on,
            "is_remote": self.is_remote(),
            "downtime": str(self.get_downtime()),
            "uptime": str(self.get_uptime()),
            "vfc": self.virtual_fail_count(),
            "info": self.last_result,
            "description": self.describe(),
            "recovery_info": self.recover_info,
            "recovered_info": self.recovered_info,
            "first_failure_time": self.first_failure_time(),
        }
        return ret

    def __str__(self) -> str:
        return self.describe()

    @property
    def type(self) -> str:
        """Compatibility with the rename of type to _type. Will be removed in the future."""
        self.monitor_logger.critical("Access to 'type' instead of '_type'!")
        return self._type


(register, get_class, all_types) = subclass_dict_handler(
    "simplemonitor.Monitors.monitor", Monitor
)


@register
class MonitorFail(Monitor):
    """A monitor which fails a fixed number of times then succeeds once, and repeats.

    Use for testing alerters etc. The default interval for successes is 5."""

    _type = "fail"

    def __init__(self, name: str, config_options: dict):
        Monitor.__init__(self, name, config_options)
        self.interval = self.get_config_option(
            "interval", required_type="int", minimum=1, default=5
        )

    def run_test(self) -> bool:
        """Always fails."""
        self.monitor_logger.info(
            "error_count = %d, interval = %d --> %d",
            self.error_count,
            self.interval,
            self.error_count % self.interval,
        )
        if (
            (self.interval == 0)
            or (self.error_count == 0)
            or (self.error_count % self.interval != 0)
        ):
            return self.record_fail("This monitor always fails.")
        else:
            return self.record_success()

    def describe(self) -> str:
        return "A monitor which always fails."

    def get_params(self) -> Tuple:
        return (self.interval,)


@register
class MonitorNull(Monitor):
    """A monitor which always passes."""

    _type = "null"

    def run_test(self) -> bool:
        return self.record_success()

    def get_params(self) -> Tuple:
        return ()
