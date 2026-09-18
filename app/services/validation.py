"""Small value conversions shared without imposing endpoint-specific policy."""
import math


def required_device_id(value):
    """Trim a nonempty string; endpoint-specific length/range rules stay local."""
    if not isinstance(value, str):
        return None
    return value.strip() or None


def finite_number(value):
    """Accept numeric strings as before, but never booleans, NaN or infinity."""
    if isinstance(value, bool):
        raise ValueError("must be a number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError("must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError("must be a finite number")
    return number


def positive_int(value, default, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        number = default
    number = max(1, number)
    return min(number, maximum) if maximum is not None else number
