'''
SPDX-License-Identifier: Apache-2.0
Copyright 2017 Massachusetts Institute of Technology.
'''

import os
import os.path
import configparser
import sys
import urllib.parse
import re
from http.server import BaseHTTPRequestHandler
import http.client

import tornado.web
import yaml
try:
    from yaml import CSafeLoader as SafeLoader
except ImportError:
    from yaml import SafeLoader

import simplejson as json


def convert(data):
    if isinstance(data, bytes):
        return data.decode()
    if isinstance(data, dict):
        return dict(map(convert, data.items()))
    if isinstance(data, tuple):
        return tuple(map(convert, data))
    if isinstance(data, list):
        return list(map(convert, data))
    return data


def environ_bool(env_name, default):
    val = os.getenv(env_name, 'default').lower()
    if val in ["on", "true", "1"]:
        return True
    if val in ["off", "false", "0"]:
        return False
    if val == "default":
        return default
    raise ValueError(
        "Environment variable %s set to invalid value "
        "%s (use either on/true/1 or off/false/0)" %
        (env_name, val))


# Current Keylime API version
API_VERSION = '2'


# SET STUB_TPM TO True TO ALLOW ALL TPM Operations to be stubbed out
# If STUB_TPM=True, TPM_CANNED_VALUES_PATH file must be provided (canned inputs)
# Canned input values can be generated by running with STUB_TPM=False and
#   specifying a TPM_CANNED_VALUES_PATH filename
STUB_TPM = False
TPM_CANNED_VALUES_PATH = None

# SET TO TRUE TO STUB A VTPM
STUB_VTPM = False
# force stub tpm if vtpm true
if STUB_VTPM:
    STUB_TPM = True

# Enable TPM benchmarking (output timing data to given file)
TPM_BENCHMARK_PATH = None

# set to False to enable keylime to run from the CWD and not require
# root access.  for testing purposes only
# all processes will log to the CWD in keylime-all.log
REQUIRE_ROOT = environ_bool('KEYLIME_REQUIRE_ROOT', True)

# enable printing of keys and other info for debug purposes
INSECURE_DEBUG = False

# allow the emuatlor to not have an ekcert even if check ekcert is true
DISABLE_EK_CERT_CHECK_EMULATOR = False

# stub out IMA functionality
STUB_IMA = False

if STUB_TPM:
    STUB_IMA = True

# allow testing mode
TEST_MODE = os.getenv('KEYLIME_TEST', 'False')
if TEST_MODE.upper() == 'TRUE':
    print("WARNING: running keylime in testing mode.\nkeylime will not run as root and ekcert checking for the TPM emulator is disabled")
    REQUIRE_ROOT = False
    DISABLE_EK_CERT_CHECK_EMULATOR = True

# whether to use tpmfs or not
MOUNT_SECURE = True

# load in JSON canned values if we're in stub mode (and JSON file given)
TPM_CANNED_VALUES = None
if STUB_TPM and TPM_CANNED_VALUES_PATH is not None:
    with open(TPM_CANNED_VALUES_PATH, "rb") as can:
        print("WARNING: using canned values in stub mode from file '%s'" %
              (TPM_CANNED_VALUES_PATH))
        # Read in JSON and strip trailing extraneous commas
        jsonInTxt = can.read().rstrip(',\r\n')
        # Saved JSON is missing surrounding braces, so add them here
        TPM_CANNED_VALUES = json.loads('{' + jsonInTxt + '}')
elif STUB_TPM:
    raise Exception(
        'STUB_TPM=True but required TPM_CANNED_VALUES_PATH not provided!')


if not REQUIRE_ROOT:
    MOUNT_SECURE = False

if not REQUIRE_ROOT:
    print("WARNING: running without root access")

# Try and import cLime, if it fails set USE_CLIME to False.
try:
    import _cLime  # pylint: disable=W0611
    USE_CLIME = True
except ImportError:
    USE_CLIME = False

TPM_LIBS_PATH = '/usr/local/lib/'
TPM_TOOLS_PATH = '/usr/local/bin/'


CONFIG_FILE = os.getenv('KEYLIME_CONFIG', '/etc/keylime.conf')


WARN = False
if not os.path.exists(CONFIG_FILE):
    # try to locate the config file next to the script if bundled
    if getattr(sys, 'frozen', False):
        CONFIG_FILE = os.path.dirname(
            os.path.abspath(sys.executable)) + "/keylime.conf"
    else:
        # instead try to get config file from python data_files install
        CONFIG_FILE = os.path.dirname(os.path.abspath(
            __file__)) + "/../package_default/keylime.conf"
        WARN = True

if not os.path.exists(CONFIG_FILE):
    raise Exception('%s does not exist. Please set environment variable KEYLIME_CONFIG or see %s for more details' % (
        CONFIG_FILE, __file__))
print(("Using config file %s" % (CONFIG_FILE,)))
if WARN:
    print("WARNING: Keylime is using the config file from its installation location. \n\tWe recommend you copy keylime.conf to /etc/ to customize it.")


_CURRENT_CONFIG = None


def get_config():
    global _CURRENT_CONFIG
    if _CURRENT_CONFIG is None:
        # read the config file
        _CURRENT_CONFIG = configparser.ConfigParser()
        _CURRENT_CONFIG.read(CONFIG_FILE)
    return _CURRENT_CONFIG


def get(section, option):
    return get_config().get(section, option)


def getint(section, option):
    return get_config().getint(section, option)


def getboolean(section, option):
    return get_config().getboolean(section, option)


def getfloat(section, option):
    return get_config().getfloat(section, option)


def has_option(section, option):
    return get_config().has_option(section, option)


if not REQUIRE_ROOT:
    DEFAULT_WORK_DIR = os.path.abspath(".")
else:
    DEFAULT_WORK_DIR = '/var/lib/keylime'
WORK_DIR = os.getenv('KEYLIME_DIR', DEFAULT_WORK_DIR)

CA_WORK_DIR = '%s/ca/' % WORK_DIR


def list_convert(data):
    if isinstance(data, bytes):
        return data.decode()
    if isinstance(data, dict):
        return dict(map(convert, data.items()))
    if isinstance(data, tuple):
        return tuple(map(convert, data))
    if isinstance(data, list):
        return list(map(convert, data))
    return data


def chownroot(path, logger):
    if os.geteuid() == 0:
        os.chown(path, 0, 0)
    elif REQUIRE_ROOT:
        logger.debug(
            "Unable to change ownership to root for file: %s" % (path))


def ch_dir(path, logger):
    if not os.path.exists(path):
        os.makedirs(path, 0o700)
        chownroot(path, logger)
    os.umask(0o077)
    os.chdir(path)


def echo_json_response(handler, code, status=None, results=None):
    """Takes a json package and returns it to the user w/ full HTTP headers"""
    if handler is None or code is None:
        return False
    if status is None:
        status = http.client.responses[code]
    if results is None:
        results = {}

    json_res = {'code': code, 'status': status, 'results': results}
    json_response = json.dumps(json_res)
    json_response = json_response.encode('utf-8')

    if isinstance(handler, BaseHTTPRequestHandler):
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json_response)
        return True
    if isinstance(handler, tornado.web.RequestHandler):
        handler.set_status(code)
        handler.set_header('Content-Type', 'application/json')
        handler.write(json_response)
        handler.finish()
        return True

    return False


def list_to_dict(alist):
    """Convert list into dictionary via grouping [k0,v0,k1,v1,...]"""
    params = {}
    i = 0
    while i < len(alist):
        params[alist[i]] = alist[i + 1] if (i + 1) < len(alist) else None
        i = i + 2
    return params


def yaml_to_dict(arry, add_newlines=True):
    arry = convert(arry)
    sep = "\n" if add_newlines else ""
    return yaml.load(sep.join(arry), Loader=SafeLoader)


def get_restful_params(urlstring):
    """Returns a dictionary of paired RESTful URI parameters"""
    parsed_path = urllib.parse.urlsplit(urlstring.strip("/"))
    query_params = urllib.parse.parse_qsl(parsed_path.query)
    path_tokens = parsed_path.path.split('/')

    # If first token is API version, ensure it isn't obsolete
    api_version = API_VERSION
    if len(path_tokens[0]) == 2 and path_tokens[0][0] == 'v':
        # Require latest API version
        if path_tokens[0][1] != API_VERSION:
            return None
        api_version = path_tokens.pop(0)

    path_params = list_to_dict(path_tokens)
    path_params["api_version"] = api_version
    path_params.update(query_params)
    return path_params


def valid_exclude_list(exclude_list):
    if not exclude_list:
        return True, None, None

    combined_regex = "(" + ")|(".join(exclude_list) + ")"
    return valid_regex(combined_regex)


def valid_regex(regex):
    if regex is None:
        return True, None, None

    try:
        compiled_regex = re.compile(regex)
    except re.error as regex_err:
        err = "Invalid regex: " + regex_err.msg + "."
        return False, None, err

    return True, compiled_regex, None


if STUB_IMA:
    IMA_ML = '../scripts/ima/ascii_runtime_measurements'
else:
    IMA_ML = '/sys/kernel/security/ima/ascii_runtime_measurements'

IMA_PCR = 10

# measured boot addons
MEASUREDBOOT_PCRS = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15]
MEASUREDBOOT_ML = '/sys/kernel/security/tpm0/binary_bios_measurements'

# this is where data will be bound to a quote, MUST BE RESETABLE!
TPM_DATA_PCR = 16

# the size of the bootstrap key for AES-GCM 256bit
BOOTSTRAP_KEY_SIZE = 32

# choose between cfssl or openssl for creating CA certificates
CA_IMPL = get_config().get('general', 'ca_implementation')

CRL_PORT = 38080
