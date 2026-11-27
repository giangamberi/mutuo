"""Single source of truth for the app version.

Read by:
  - desktop.py (the update check compares this against the latest git tag)
  - build_installer.ps1 (writes it into installer/Mutuo.iss at build time)

Bump this when cutting a release, then rebuild the installer.
"""

__version__ = "0.7.2"
