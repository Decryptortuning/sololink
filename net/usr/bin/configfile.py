#!/usr/bin/env python3

# Get and set parameters in a configuration file (e.g. /etc/hostapd.conf)

import re
import os

# Assumptions (all derived from looking at a sample hostapd.conf file):
#
# A valid parameter name is [a-zA-Z0-9_]+
#
# A valid parameter value is everything after the '=' to the end of the line,
# whitespace trimmed on the ends. Can contain whitespace, ., :, etc. Can't
# handle a comment at the end of a setting; it will be taken as part of the
# setting.
#
# A "parameter setting" is:
# - optional [\t ]*
# - valid parameter name
# - optional [\t ]*
# - '='
# - valid parameter value
#
# A "commented out parameter setting" is:
# - optional [\t ]*
# - '#'
# - "parameter setting"

def paramGet(configFileName, paramName):
    '''
    Read parameter setting from config file
    '''
    configFile = open(configFileName) # IOError

    # process line-by-line
    for line in configFile:
        # re.match only works at beginning of line (we want that)
        m = re.match('[\t ]*' + paramName + '[\t ]*=[\t ]*(.*?)[\t ]*\n', line)
        if m:
            return m.group(1)

    # not found
    return None


def paramSet(configFileName, paramName, paramValue):
    from pathlib import Path
    import opensolo_wifi
    path = Path(configFileName).absolute()
    opensolo_wifi.safe_path(path.parent, path.name)
    text = opensolo_wifi.replace_assignment(path.read_text(), paramName, str(paramValue))
    if path.name == "hostapd.conf" and path.parent.name == "etc":
        opensolo_wifi.save(path.parent.parent, "hostapd", text)
    else:
        opensolo_wifi.atomic(path, text)
