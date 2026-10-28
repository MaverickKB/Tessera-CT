"""Locate shipped runtime assets in a wheel or an editable source checkout."""

from pathlib import Path


def _installed_layout(package):
    """A wheel install carries the build stamp. An editable checkout does not.

    The stamp is written for every non-editable build, including one whose
    engine files failed to land. Its presence is what stops asset lookup from
    walking out of the package into a neighboring checkout.
    """
    return (package / '_assets' / 'build-stamp.json').is_file()


def asset_path(name):
    package = Path(__file__).resolve().parent
    bundled = package / '_assets' / name
    if bundled.exists():
        return bundled
    # An installed package must find the engine inside the package. Falling
    # through to package.parent hid a wheel that shipped no engine whenever a
    # checkout happened to sit beside site-packages.
    if _installed_layout(package):
        return bundled
    return package.parent / name
