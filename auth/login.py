import sys

IS_NETBSD = sys.platform.startswith("netbsd")

# NetBSD fix: python-pam loads libpam with a local dlopen scope, but the
# NetBSD PAM modules (pam_unix.so, pam_self.so, etc.) resolve symbols such
# as pam_error and _pam_verbose_error from libpam at load time. Without a
# global scope, every module fails to load and pam_start() returns
# PAM_SYSTEM_ERR. Pre-loading libpam with RTLD_GLOBAL before python-pam
# uses it makes those symbols visible to the modules. This block only
# runs on NetBSD and cannot affect Linux or FreeBSD.
if IS_NETBSD:
    import ctypes
    import ctypes.util

    _pam_library = ctypes.util.find_library("pam")
    if _pam_library:
        ctypes.CDLL(_pam_library, mode=ctypes.RTLD_GLOBAL)

import pam


def authenticate_user(username: str, password: str) -> bool:
    if IS_NETBSD:
        # NetBSD fix: the default "login" PAM service includes
        # pam_securetty in its account phase, which refuses root logins
        # without a secure TTY (a web application has no TTY). The
        # "system" service authenticates against the same user database
        # without the TTY restriction.
        return pam.authenticate(username, password, service="system")
    # Linux and FreeBSD: unchanged original call (python-pam defaults
    # to the "login" service).
    return pam.authenticate(username, password)
