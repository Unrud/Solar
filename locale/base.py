import math


class BaseTranslation:
    lang = "en"
    no_value = "?"
    decimal_seperator = "."
    thousands_seperator = ","
    strings = {
        "{} hr": None,
        "{} hr {} min": None,
        "{} min": None,
        "Apply": None,
        "auto": None,
        "Auto": None,
        "Automatic": None,
        "Automatic shutdown": None,
        "Battery": None,
        "Buzzer": None,
        "bypassed": None,
        "Bypass": None,
        "Charge level": None,
        "charging": None,
        "Charging power": None,
        "discharging": None,
        "Discharging power": None,
        "Electricity meter": None,
        "Error": None,
        "Hub": None,
        "inactive": None,
        "Inverter manufacturer": None,
        "Maximum charge level": None,
        "Maximum inverter power": None,
        "Maximum power": None,
        "Maximum temperature": None,
        "Minimum charge level": None,
        "New limit": None,
        "No connection": None,
        "Off": None,
        "On": None,
        "Other": None,
        "Output": None,
        "Pack {}": None,
        "Panel {}": None,
        "Power": None,
        "Power import": None,
        "Remaining at limit": None,
        "Reset": None,
        "Reset bypass to auto after one day": None,
        "Serial number": None,
        "Settings": None,
        "Software version": None,
        "Solar": None,
        "State of health": None,
        "Target range": None,
        "Total power": None,
        "Total power consumption": None,
        "value must be >= 100 or a multiple of 30": None,
    }

    def __call__(self, s, *args, raw=False):
        localized = self.strings.get(s)
        localized = s if localized is None else localized
        if raw:
            return localized
        return localized.format(*args)

    def minutes(self, value):
        t = self
        if value is None:
            return self.no_value
        value = math.trunc(value)
        if value // 60 == 0:
            return t("{} min", value)
        if value % 60 == 0:
            return t("{} hr", value // 60)
        return t("{} hr {} min", value // 60, value % 60)

    def number(self, value, unit=None, round=0, div=1):
        if value is None:
            return self.no_value
        value /= div
        s = f"{{:.{round}f}}".format(value).lstrip("-")
        p = s.find(".")
        if p == -1:
            p = len(s)
        s = s.replace(".", self.decimal_seperator)
        for i in range(p - 3, 0, -3):
            s = s[:i] + self.thousands_seperator + s[i:]
        if value < 0:
            s = f"-{s}"
        if unit:
            s += f" {unit}"
        return s

    def number_range(self, value1, value2, unit="", round=0, div=1):
        if value1 is None and value2 is None:
            return self.no_value
        s = (
            f"{self.number(value1, '', round, div)} "
            + f"- {self.number(value2, '', round, div)}"
        )
        if unit:
            s += f" {unit}"
        return s
