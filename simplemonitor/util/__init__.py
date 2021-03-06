"""Utilities for SimpleMonitor."""

import datetime
import json
import re
import socket
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from .envconfig import EnvironmentAwareConfigParser


class MonitorConfigurationError(ValueError):
    """A config error for a Monitor"""

    pass


class AlerterConfigurationError(ValueError):
    """A config error for an Alerter"""

    pass


class LoggerConfigurationError(ValueError):
    """A config error for a Logger"""

    pass


class SimpleMonitorConfigurationError(ValueError):
    """A general config error"""

    pass


class MonitorState(Enum):
    UNKNOWN = 0  # state not known yet
    SKIPPED = 1  # monitor was skipped
    OK = 2  # monitor is ok
    FAILED = 3  # monitor has failed


class UpDownTime:
    """Represent an up- or downtime"""

    days = 0
    hours = 0
    minutes = 0
    seconds = 0

    def __init__(
        self, days: int = 0, hours: int = 0, minutes: int = 0, seconds: int = 0
    ) -> None:
        if not isinstance(days, int):
            raise TypeError("days must be an int")
        if not isinstance(hours, int):
            raise TypeError("days must be an int")
        if not isinstance(minutes, int):
            raise TypeError("days must be an int")
        if not isinstance(seconds, int):
            raise TypeError("days must be an int")
        self.days = days
        self.hours = hours
        self.minutes = minutes
        self.seconds = seconds

    def __str__(self) -> str:
        """Format as d+h:m:s"""
        return "{}+{:02}:{:02}:{:02}".format(
            self.days, self.hours, self.minutes, int(self.seconds)
        )

    def __repr__(self) -> str:
        return "<{}: {}>".format(self.__class__, self.__str__())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, UpDownTime):
            return NotImplemented
        if (
            self.days == other.days
            and self.hours == other.hours
            and self.minutes == other.minutes
            and self.seconds == other.seconds
        ):
            return True
        return False

    @staticmethod
    def from_timedelta(td: datetime.timedelta) -> "UpDownTime":
        """Generate an UpDownTime from a timedelta object"""
        if td is None:
            return UpDownTime()
        else:
            downtime_seconds = td.seconds
            (hours, minutes) = (0, 0)
            if downtime_seconds > 3600:
                (hours, downtime_seconds) = divmod(downtime_seconds, 3600)
            if downtime_seconds > 60:
                (minutes, downtime_seconds) = divmod(downtime_seconds, 60)
            return UpDownTime(td.days, hours, minutes, downtime_seconds)


def get_config_option(
    config_options: dict, key: str, **kwargs: Any
) -> Union[None, str, int, float, bool, List[str], List[int]]:
    """Get a value out of a dict, with possible default, required type and requiredness."""
    exception = kwargs.get("exception", ValueError)

    if not isinstance(config_options, dict):
        raise exception("config_options should be a dict")

    default = kwargs.get("default", None)
    required = kwargs.get("required", False)
    value = config_options.get(key, default)
    if required and value is None:
        raise exception("config option {0} is missing and is required".format(key))
    required_type = kwargs.get("required_type", "str")
    allowed_values = kwargs.get("allowed_values", None)
    allow_empty = kwargs.get("allow_empty", True)
    if isinstance(value, str) and required_type:
        if required_type == "str" and value == "" and not allow_empty:
            raise exception("config option {0} cannot be empty".format(key))
        if required_type in ["int", "float"]:
            try:
                if required_type == "int":
                    value = int(value)
                else:
                    value = float(value)
            except ValueError:
                raise exception(
                    "config option {0} needs to be an {1}".format(key, required_type)
                )
            minimum = kwargs.get("minimum")
            if minimum is not None and value < minimum:
                raise exception(
                    "config option {0} needs to be >= {1}".format(key, minimum)
                )
            maximum = kwargs.get("maximum")
            if maximum is not None and value > maximum:
                raise exception(
                    "config option {0} needs to be <= {1}".format(key, maximum)
                )
        if required_type == "[int]":
            try:
                value = [int(x) for x in value.split(",")]
            except ValueError:
                raise exception(
                    "config option {0} needs to be a list of int[int,...]".format(key)
                )
        if required_type == "bool":
            value = bool(value.lower() in ["1", "true", "yes"])
        if required_type == "[str]":
            value = [x.strip() for x in value.split(",")]
    if isinstance(value, list) and allowed_values:
        if not all([x in allowed_values for x in value]):
            raise exception(
                "config option {0} needs to be one of {1}".format(key, allowed_values)
            )
    else:
        if allowed_values is not None and value not in allowed_values:
            raise exception(
                "config option {0} needs to be one of {1}".format(key, allowed_values)
            )
    return value


def format_datetime(the_datetime: Optional[datetime.datetime]) -> str:
    """Return an isoformat()-like datetime without the microseconds."""
    if the_datetime is None:
        return ""

    if isinstance(the_datetime, datetime.datetime):
        the_datetime = the_datetime.replace(microsecond=0)
        return the_datetime.isoformat(" ")
    return the_datetime


def short_hostname() -> str:
    """Get just our machine name.

    TODO: This might actually be redundant. Python probably provides it's own version of this."""

    return (socket.gethostname() + ".").split(".")[0]


def get_config_dict(
    config: EnvironmentAwareConfigParser, monitor: str
) -> Dict[str, str]:
    options = config.items(monitor)
    ret = {}
    for (key, value) in options:
        ret[key] = value
    return ret


DATETIME_MAGIC_TOKEN = "__simplemonitor_datetime"  # nosec
MONITORSTATE_MAGIC_TOKEN = "__simplemonitor_monitorstate"  # nosec
FORMAT = "%Y-%m-%d %H:%M:%S.%f"


class JSONEncoder(json.JSONEncoder):
    _regexp_type = type(re.compile(""))

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime.datetime):
            return {DATETIME_MAGIC_TOKEN: obj.strftime(FORMAT)}
        if isinstance(obj, self._regexp_type):
            return "<removed compiled regexp object>"
        if isinstance(obj, MonitorState):
            return {MONITORSTATE_MAGIC_TOKEN: obj.name}
        return super(JSONEncoder, self).default(obj)


class JSONDecoder(json.JSONDecoder):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._original_object_pairs_hook = kwargs.pop("object_pairs_hook", None)
        kwargs["object_pairs_hook"] = self.object_pairs_hook
        super(JSONDecoder, self).__init__(*args, **kwargs)

    _datetime_re = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{6}")

    def object_pairs_hook(self, obj: Any) -> Any:
        if (
            len(obj) == 1
            and obj[0][0] == DATETIME_MAGIC_TOKEN
            and isinstance(obj[0][1], str)
            and self._datetime_re.match(obj[0][1])
        ):
            return datetime.datetime.strptime(obj[0][1], FORMAT)
        elif (
            len(obj) == 1
            and obj[0][0] == MONITORSTATE_MAGIC_TOKEN
            and isinstance(obj[0][1], str)
        ):
            return MonitorState[obj[0][1]]
        elif self._original_object_pairs_hook:
            return self._original_object_pairs_hook(obj)
        else:
            return dict(obj)


def json_dumps(data: Any) -> bytes:
    return JSONEncoder().encode(data).encode("ascii")


def json_loads(string: bytes) -> str:
    return JSONDecoder().decode(string.decode("ascii"))


def subclass_dict_handler(
    mod: str, base_cls: type
) -> Tuple[Callable, Callable, Callable]:
    def _check_is_subclass(cls: Any) -> None:
        if not issubclass(cls, base_cls):
            raise TypeError(
                ("%s.register may only be used on subclasses " "of %s.%s")
                % (mod, mod, base_cls.__name__)
            )

    _subclasses = {}

    def register(cls: Any) -> Any:
        """Decorator for monitor classes."""
        _check_is_subclass(cls)
        if cls is None or cls._type == "unknown":
            raise ValueError("Cannot register this class")
        _subclasses[cls._type] = cls
        return cls

    def get_class(type_: Any) -> Any:
        return _subclasses[type_]

    def all_types() -> list:
        return list(_subclasses)

    return (register, get_class, all_types)
