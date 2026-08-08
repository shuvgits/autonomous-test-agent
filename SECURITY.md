# Security Policy

## Sandbox Isolation

This tool executes model-generated Python code. All execution happens in a sandbox to contain untrusted model output.

**Docker is required for security guarantees.** `DockerSandbox` provides real isolation with separate namespaces, disabled networking, read-only root filesystem, capability dropping, and hard resource limits.

**`SubprocessSandbox` is development-only.** It uses POSIX resource limits and temporary directories. This stops runaway resource use and accidental damage, but it is not a security boundary: the same process tree has access to the user's files and environment. Use subprocess mode only in trusted development environments.

## Usage Guidance

- **For untrusted model-generated code:** Use `--sandbox docker` (default, preferred).
- **For local development:** `--sandbox subprocess` is acceptable.
- **On macOS with subprocess mode:** Isolation is weaker than Linux. Fork bombs and memory bombs are constrained by wall-clock timeout, not hard limits. Use Docker for any code you would not run unsandboxed.

The tool prefers Docker and prints a warning if it falls back, because silently downgrading isolation is how incidents happen.

## Reporting Security Issues

Please do not report security vulnerabilities through public GitHub issues.

Report vulnerabilities privately through GitHub Security Advisories:
https://github.com/shuvgits/autonomous-test-agent/security/advisories/new

Please include:
- A clear description of the issue
- Steps to reproduce
- Potential impact
- Any suggested mitigation, if available

I will acknowledge reports within 7 days and aim to provide a fix or status update within 30 days.

## Environment Isolation

The sandbox blocks parent process environment inheritance by design. The child process builds its environment from scratch, preventing model-generated code from reading API keys or other sensitive values from the parent shell.

## Supported Versions

Security fixes are provided for the latest version on the `main` branch.

## Disclosure Policy

Please allow reasonable time for a fix before publicly disclosing a vulnerability. Once a fix is available, I will document the issue and remediation in the release notes where appropriate.