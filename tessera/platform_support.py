"""Facts about this operating system for Settings.

Each sentence is something the running process can stand behind. A feature
that is not implemented here is named as absent. Nothing in this module is a
promise about a machine this process is not running on.
"""

import sys


def host_report():
    system = sys.platform
    if system == 'darwin':
        platform = 'macos'
        limits = [
            'LaunchAgent start and stop is available when a LaunchAgent plist is configured. Otherwise this process is the core.',
            'Credentials can come from an environment variable or the macOS keychain.',
            'Desktop computer use uses macOS facilities. A missing permission is reported as unavailable.',
        ]
    elif system.startswith('linux'):
        platform = 'linux'
        limits = [
            'This process is the core. LaunchAgent start and stop is macOS-only and is not available on Linux.',
            'Credentials can come from an environment variable or secret-tool. If secret-tool is not installed, a keychain lookup says the helper is unavailable.',
            'Desktop computer use uses the Linux tools that are installed. A missing tool or display is reported as unavailable.',
        ]
    elif system.startswith('win'):
        platform = 'windows'
        limits = [
            'This process is the core. LaunchAgent start and stop is macOS-only and is not available on Windows. This window does not install a Windows service.',
            'Credentials on Windows come from environment variables. The macOS keychain and Linux secret-tool are not available, and Windows Credential Manager is not used.',
            'Desktop computer use is not supported on Windows. Those tools report unavailable instead of acting.',
            'Windows is supported by running Tessera under WSL, where this program runs as Linux. Native Windows is not yet supported for embedded memory: the embedded engine needs file locking that native Windows lacks.',
        ]
    else:
        platform = system
        limits = [
            'This process is the core. System service controls are not available on this operating system.',
            'Desktop computer use is not supported on this operating system. Those tools report unavailable instead of acting.',
        ]
    return {'platform': platform, 'limits': limits}
