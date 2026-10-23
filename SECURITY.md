# Security policy

## Reporting a vulnerability

Please do not open a public issue for a security problem. Report it privately through
[GitHub's private vulnerability reporting](https://github.com/Rouzax/home-connect-domoticz-plugin/security/advisories/new).

Include what you found, how to reproduce it, and what an attacker could do with it. Do not
include your own Client ID, Client Secret, tokens or appliance IDs.

## Supported versions

Fixes go into the latest release. During the beta, that is the newest 0.1.0 pre-release.

## What the plugin stores

The Home Connect tokens, and the Client Secret if you entered one, are stored in the Domoticz
database in plain text. The
[Security page](https://rouzax.github.io/home-connect-domoticz-plugin/security/) explains what
that gives access to and how to withdraw it.
