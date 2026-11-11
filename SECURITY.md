# Security and responsible use

cyberdeckji is a local Linux launcher with optional network diagnostics.
Only inspect networks you own or are explicitly authorized to test. Discovery
and port scans generate traffic and may affect sensitive or fragile devices.
The project does not authorize attacks or provide a guarantee of safe scanning.

## Reporting vulnerabilities

Once this repository is published on GitHub, use its Security tab's
"Report a vulnerability" action if private vulnerability reporting is enabled.
Maintainers should enable that feature before publication.

If private reporting is unavailable, open an issue requesting a private contact
route without including exploit instructions, sensitive logs, credentials or
details of an unpatched vulnerability. Do not assume an issue is private.

Include the affected revision, a minimal reproduction using synthetic data,
expected behavior and impact when a private channel is available.

## Abuse concerns

Open a repository issue for questions about potentially harmful use or content.
Do not publish third-party network identifiers or personal information.
For an ongoing attack involving GitHub infrastructure, use
[GitHub's abuse reporting process](https://docs.github.com/en/site-policy/acceptable-use-policies/github-active-malware-or-exploits).

## Trust boundaries

Native apps and custom Python/Bash scripts are trusted local code, not sandboxed
plugins. Review scripts before installing them; forms are not a security boundary.
The launcher does not automatically elevate privileges. Configure any required
packet-access permissions deliberately through your system administrator.
