<#
  .SYNOPSIS
    Windows entry point for installing the Graphify login-time launcher.

  .DESCRIPTION
    Delegates to install_autostart.py, which selects the startup mechanism of
    the host platform.
#>
$ErrorActionPreference = 'Stop'
. (Join-Path (Join-Path $PSScriptRoot '..') 'common.ps1')

Invoke-AiMemoryPythonScript -Script (Join-Path $PSScriptRoot 'install_autostart.py')
